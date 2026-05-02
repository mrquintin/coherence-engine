"""Investor (LP) accreditation-verification routes (prompt 26).

Two surfaces:

* **Founder/operator portal** — ``POST /investors/{id}/verification:initiate``
  and ``GET /investors/{id}/verification`` are gated by
  :func:`current_investor`, which mirrors :func:`current_founder` from
  prompt 25 but resolves an :class:`Investor` row keyed by Supabase
  ``sub``. The service-role bypass (``admin``/``analyst``/``viewer``)
  is honored for operator tooling.

* **Provider webhooks** — ``POST /webhooks/persona`` and
  ``POST /webhooks/onfido`` accept signed deliveries from the
  configured providers. Signature verification is HMAC-SHA-256 over
  the raw request body (``hmac.compare_digest`` for constant-time
  comparison) with a 5-minute timestamp-skew check; an invalid
  signature returns 401 and does NOT mutate any row.

Notes
-----

The webhook routes are deliberately mounted *outside* the
``/investors/...`` prefix so they are not gated by the founder JWT
middleware — providers do not carry user JWTs, only signed payloads.
The ``FundSecurityMiddleware`` API-key gate also skips
``/api/v1/webhooks/...`` paths; the signature on the body is the
only authentication.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Path, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import (
    envelope,
    error_response,
    new_request_id,
)
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.security import audit_log, enforce_roles
from coherence_engine.server.fund.security.auth import (
    AuthError,
    verify_supabase_jwt,
)
from coherence_engine.server.fund.services.accredited_backends import (
    AccreditedBackend,
    AccreditedBackendConfigError,
    AccreditedBackendError,
    backend_for_provider,
)
from coherence_engine.server.fund.services.accredited_verification import (
    VerificationError,
    apply_webhook,
    evaluate_effective_status,
    initiate_verification,
    latest_record_for_investor,
)


router = APIRouter(tags=["investor_verification"])

LOGGER = logging.getLogger("coherence_engine.fund.investor_verification")

_SERVICE_ROLES = {"admin", "analyst", "viewer"}


# ---------------------------------------------------------------------------
# Backend factory injection point — lets tests replace
# ``backend_for_provider`` with a fake without monkey-patching the import.
# ---------------------------------------------------------------------------

_BACKEND_FACTORY = backend_for_provider


def set_backend_factory_for_tests(factory) -> None:
    """Override the backend factory (test-only seam)."""
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = factory


def reset_backend_factory_for_tests() -> None:
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = backend_for_provider


def _resolve_backend(provider: str) -> AccreditedBackend:
    return _BACKEND_FACTORY(provider)


# ---------------------------------------------------------------------------
# current_investor dependency
# ---------------------------------------------------------------------------


def _upsert_investor(db: Session, sub: str) -> models.Investor:
    existing = (
        db.query(models.Investor)
        .filter(models.Investor.founder_user_id == sub)
        .one_or_none()
    )
    if existing is not None:
        return existing
    investor = models.Investor(
        id=f"inv_{sub[:32]}",
        founder_user_id=sub,
        legal_name="",
        residence_country="",
        investor_type="individual",
        status="unverified",
    )
    db.add(investor)
    try:
        db.flush()
    except Exception:
        db.rollback()
        existing = (
            db.query(models.Investor)
            .filter(models.Investor.founder_user_id == sub)
            .one_or_none()
        )
        if existing is not None:
            return existing
        raise
    return investor


def current_investor(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[models.Investor]:
    """FastAPI dependency: verify Bearer JWT and return the Investor row.

    Mirrors :func:`current_founder` from prompt 25:

    * **JWT path**: verify token, lazily upsert an ``Investor`` row keyed
      by the Supabase ``sub`` claim, return it.
    * **Service-role bypass**: middleware-authenticated principal with
      role ``admin``/``analyst``/``viewer`` returns ``None`` so route
      handlers can skip ownership checks.
    * Otherwise raises 401.
    """
    auth = request.headers.get("authorization", "")
    has_bearer = auth.lower().startswith("bearer ")
    if not has_bearer:
        principal = getattr(request.state, "principal", None) or {}
        role = str(principal.get("role", "")).lower()
        if role in _SERVICE_ROLES:
            return None
        raise AuthError(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "missing bearer token"},
        )

    token = auth[7:].strip()
    claims = verify_supabase_jwt(token)
    sub = str(claims["sub"])
    investor = _upsert_investor(db, sub=sub)
    db.commit()
    request.state.principal = {
        "auth_type": "supabase_jwt",
        "role": "investor",
        "investor_id": investor.id,
        "fingerprint": f"sub={sub[:36]}",
        "key_id": None,
    }
    return investor


def _investor_owns(investor: Optional[models.Investor], target: models.Investor) -> bool:
    if investor is None:
        return True  # service-role caller
    return str(investor.id) == str(target.id)


# ---------------------------------------------------------------------------
# Routes — investor portal
# ---------------------------------------------------------------------------


@router.post("/investors/{investor_id}/verification:initiate", status_code=201)
def initiate_verification_route(
    investor_id: str = Path(...),
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    investor=Depends(current_investor),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    if investor is None:
        denied = enforce_roles(request, ("analyst", "admin"))
        if denied:
            return denied

    target = (
        db.query(models.Investor).filter(models.Investor.id == investor_id).one_or_none()
    )
    if target is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "investor not found"
        )
    if not _investor_owns(investor, target):
        return error_response(
            request_id, 403, "FORBIDDEN", "investor not owned by caller"
        )

    provider = str(body.get("provider") or "").strip().lower()
    if provider not in {"persona", "onfido", "manual"}:
        return error_response(
            request_id,
            400,
            "VALIDATION_ERROR",
            "provider must be one of persona|onfido|manual",
            details=[{"field": "provider", "issue": "invalid"}],
        )
    redirect_url = body.get("redirect_url")
    if redirect_url is not None and not isinstance(redirect_url, str):
        return error_response(
            request_id,
            400,
            "VALIDATION_ERROR",
            "redirect_url must be a string",
        )

    try:
        backend = _resolve_backend(provider)
    except AccreditedBackendConfigError as exc:
        return error_response(
            request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
        )
    except (ValueError, AccreditedBackendError) as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )

    try:
        record = initiate_verification(
            db,
            investor=target,
            backend=backend,
            redirect_url=redirect_url,
        )
    except VerificationError as exc:
        return error_response(
            request_id, 502, "PROVIDER_ERROR", str(exc)
        )

    # Re-run the backend.initiate to harvest the upload token / redirect URL
    # for the response shape — but note that ``initiate_verification`` already
    # captured the provider_reference and persisted it. We surface the same
    # provider_reference from the row so callers can correlate later.
    response_payload = envelope(
        request_id=request_id,
        data={
            "record_id": record.id,
            "provider": record.provider,
            "provider_reference": record.provider_reference,
            "status": record.status,
            "redirect_url": _initiation_redirect_for_record(backend, record, redirect_url),
        },
    )
    db.commit()
    audit_log(
        "investor_verification_initiate",
        request,
        "allowed",
        {"investor_id": target.id, "provider": provider, "record_id": record.id},
    )
    return response_payload


def _initiation_redirect_for_record(
    backend: AccreditedBackend,
    record: models.VerificationRecord,
    redirect_url: Optional[str],
) -> str:
    """Re-derive the redirect URL for the response.

    ``initiate_verification`` persists the provider reference but not
    the redirect URL (the URL is a derived value the provider can
    rotate). For the canned synthetic backends shipped in-tree the
    URL is deterministic from the reference; in production the
    operator UI calls the provider's hosted-flow endpoint directly.
    """
    name = backend.name
    ref = record.provider_reference
    if name == "persona":
        return redirect_url or f"https://withpersona.com/verify?inquiry-id={ref}"
    if name == "onfido":
        return redirect_url or f"https://onfido.com/verify?check_id={ref}"
    return ""


@router.get("/investors/{investor_id}/verification")
def get_verification_route(
    investor_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    investor=Depends(current_investor),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    if investor is None:
        denied = enforce_roles(request, ("viewer", "analyst", "admin"))
        if denied:
            return denied

    target = (
        db.query(models.Investor).filter(models.Investor.id == investor_id).one_or_none()
    )
    if target is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "investor not found"
        )
    if not _investor_owns(investor, target):
        return error_response(
            request_id, 403, "FORBIDDEN", "investor not owned by caller"
        )

    record = latest_record_for_investor(db, target.id)
    effective = evaluate_effective_status(record)
    if record is None:
        return envelope(
            request_id=request_id,
            data={
                "investor_id": target.id,
                "status": "absent",
                "record": None,
            },
        )
    return envelope(
        request_id=request_id,
        data={
            "investor_id": target.id,
            "status": effective,
            "record": {
                "record_id": record.id,
                "provider": record.provider,
                "method": record.method,
                "status": record.status,
                "expires_at": record.expires_at.isoformat()
                if record.expires_at
                else None,
                "completed_at": record.completed_at.isoformat()
                if record.completed_at
                else None,
                "provider_reference": record.provider_reference,
            },
        },
    )


# ---------------------------------------------------------------------------
# Webhook routes
# ---------------------------------------------------------------------------


async def _read_raw_body(request: Request) -> bytes:
    return await request.body()


def _handle_webhook(
    provider: str,
    raw: bytes,
    headers: Dict[str, str],
    db: Session,
    *,
    trace_id: str,
) -> Dict[str, object]:
    """Common webhook handler — verify signature, apply update, surface result."""
    try:
        backend = _resolve_backend(provider)
    except (AccreditedBackendConfigError, AccreditedBackendError, ValueError) as exc:
        # Provider misconfiguration on our side is a 503 — never silently
        # accept an unverifiable webhook.
        raise VerificationError(f"backend_unavailable:{exc}") from exc
    return _apply_and_format(backend, raw, headers, db, trace_id=trace_id)


def _apply_and_format(
    backend: AccreditedBackend,
    raw: bytes,
    headers: Dict[str, str],
    db: Session,
    *,
    trace_id: str,
) -> Dict[str, object]:
    record = apply_webhook(
        db,
        backend=backend,
        raw_payload=raw,
        headers=headers,
        trace_id=trace_id,
    )
    if record is None:
        return {"status": "ignored", "record_id": None}
    return {
        "status": record.status,
        "record_id": record.id,
        "method": record.method,
    }


@router.post("/webhooks/persona")
async def persona_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    raw = await _read_raw_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    trace_id = headers.get("x-request-id") or f"trace_{uuid.uuid4().hex[:12]}"
    request_id = headers.get("x-request-id") or new_request_id()
    try:
        result = _handle_webhook("persona", raw, headers, db, trace_id=trace_id)
    except VerificationError as exc:
        if str(exc).startswith("webhook_signature_invalid"):
            return error_response(
                request_id, 401, "UNAUTHORIZED", "invalid webhook signature"
            )
        if str(exc).startswith("backend_unavailable"):
            return error_response(
                request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
            )
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )
    db.commit()
    return envelope(request_id=request_id, data=result)


@router.post("/webhooks/onfido")
async def onfido_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    raw = await _read_raw_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    trace_id = headers.get("x-request-id") or f"trace_{uuid.uuid4().hex[:12]}"
    request_id = headers.get("x-request-id") or new_request_id()
    try:
        result = _handle_webhook("onfido", raw, headers, db, trace_id=trace_id)
    except VerificationError as exc:
        if str(exc).startswith("webhook_signature_invalid"):
            return error_response(
                request_id, 401, "UNAUTHORIZED", "invalid webhook signature"
            )
        if str(exc).startswith("backend_unavailable"):
            return error_response(
                request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
            )
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )
    db.commit()
    return envelope(request_id=request_id, data=result)
