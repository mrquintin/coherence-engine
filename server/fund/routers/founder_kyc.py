"""Founder KYC/AML routes (prompt 53).

Two surfaces:

* **Operator portal** -- ``POST /founders/{id}/kyc:initiate`` and
  ``GET /founders/{id}/kyc`` are gated by the founder JWT (or a
  service-role principal: admin / analyst / viewer) so the founder
  themself can drive their own screening flow and the operator UI can
  read status. The handlers do not collide with the LP-flow routes
  in :mod:`investor_verification` -- those are mounted under
  ``/investors/...``, these under ``/founders/...``.

* **Provider webhooks** -- ``POST /webhooks/founder_kyc/persona`` and
  ``POST /webhooks/founder_kyc/onfido`` accept signed deliveries
  from the configured providers. Signature verification is HMAC-SHA-256
  over the raw request body (``hmac.compare_digest`` for constant-time
  comparison) with a 5-minute timestamp-skew check; an invalid
  signature returns 401 and does NOT mutate any row.

Notes
-----

The webhook routes are mounted *outside* any founder-JWT middleware
because providers do not carry user JWTs, only signed payloads. The
``FundSecurityMiddleware`` API-key gate also skips
``/api/v1/webhooks/...`` paths; the signature on the body is the only
authentication.
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
from coherence_engine.server.fund.services.founder_kyc import (
    KYCError,
    apply_webhook,
    evaluate_effective_status,
    initiate_kyc,
    latest_result_for_founder,
)
from coherence_engine.server.fund.services.founder_kyc_backends import (
    FounderKYCBackend,
    FounderKYCBackendConfigError,
    FounderKYCBackendError,
    kyc_backend_for_provider,
)


router = APIRouter(tags=["founder_kyc"])

LOGGER = logging.getLogger("coherence_engine.fund.founder_kyc")

_SERVICE_ROLES = {"admin", "analyst", "viewer"}


# ---------------------------------------------------------------------------
# Backend factory injection point -- lets tests replace
# ``kyc_backend_for_provider`` with a fake without monkey-patching.
# ---------------------------------------------------------------------------

_BACKEND_FACTORY = kyc_backend_for_provider


def set_kyc_backend_factory_for_tests(factory) -> None:
    """Override the KYC backend factory (test-only seam)."""
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = factory


def reset_kyc_backend_factory_for_tests() -> None:
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = kyc_backend_for_provider


def _resolve_backend(provider: str) -> FounderKYCBackend:
    return _BACKEND_FACTORY(provider)


# ---------------------------------------------------------------------------
# current_founder dependency (KYC-flavored)
# ---------------------------------------------------------------------------


def current_founder_kyc(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[models.Founder]:
    """FastAPI dependency: verify Bearer JWT and return the Founder row.

    * **JWT path**: verify token, look up the ``Founder`` row by the
      Supabase ``sub`` claim, return it. We do not lazily upsert a
      founder here (unlike the LP investor flow) -- founders are
      created via the application intake path.
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
    founder = (
        db.query(models.Founder)
        .filter(models.Founder.founder_user_id == sub)
        .one_or_none()
    )
    if founder is None:
        raise AuthError(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "founder not found"},
        )
    request.state.principal = {
        "auth_type": "supabase_jwt",
        "role": "founder",
        "founder_id": founder.id,
        "fingerprint": f"sub={sub[:36]}",
        "key_id": None,
    }
    return founder


def _founder_owns(
    founder: Optional[models.Founder], target: models.Founder
) -> bool:
    if founder is None:
        return True  # service-role caller
    return str(founder.id) == str(target.id)


# ---------------------------------------------------------------------------
# Routes -- founder portal
# ---------------------------------------------------------------------------


@router.post("/founders/{founder_id}/kyc:initiate", status_code=201)
def initiate_kyc_route(
    founder_id: str = Path(...),
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    founder=Depends(current_founder_kyc),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    if founder is None:
        denied = enforce_roles(request, ("analyst", "admin"))
        if denied:
            return denied

    target = (
        db.query(models.Founder)
        .filter(models.Founder.id == founder_id)
        .one_or_none()
    )
    if target is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "founder not found"
        )
    if not _founder_owns(founder, target):
        return error_response(
            request_id, 403, "FORBIDDEN", "founder not owned by caller"
        )

    provider = str(body.get("provider") or "").strip().lower()
    if provider not in {"persona", "onfido"}:
        return error_response(
            request_id,
            400,
            "VALIDATION_ERROR",
            "provider must be one of persona|onfido",
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
    except FounderKYCBackendConfigError as exc:
        return error_response(
            request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
        )
    except (ValueError, FounderKYCBackendError) as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )

    try:
        record = initiate_kyc(
            db,
            founder=target,
            backend=backend,
            redirect_url=redirect_url,
            screening_categories=body.get("screening_categories"),
        )
    except KYCError as exc:
        return error_response(
            request_id, 502, "PROVIDER_ERROR", str(exc)
        )

    response_payload = envelope(
        request_id=request_id,
        data={
            "result_id": record.id,
            "provider": record.provider,
            "provider_reference": record.provider_reference,
            "status": record.status,
            "redirect_url": _initiation_redirect_for_record(
                backend, record, redirect_url
            ),
            "screening_categories": record.screening_categories,
        },
    )
    db.commit()
    audit_log(
        "founder_kyc_initiate",
        request,
        "allowed",
        {
            "founder_id": target.id,
            "provider": provider,
            "result_id": record.id,
        },
    )
    return response_payload


def _initiation_redirect_for_record(
    backend: FounderKYCBackend,
    record: models.KYCResult,
    redirect_url: Optional[str],
) -> str:
    name = backend.name
    ref = record.provider_reference
    if name == "persona":
        return redirect_url or f"https://withpersona.com/kyc?inquiry-id={ref}"
    if name == "onfido":
        return redirect_url or f"https://onfido.com/kyc?check_id={ref}"
    return ""


@router.get("/founders/{founder_id}/kyc")
def get_kyc_route(
    founder_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    founder=Depends(current_founder_kyc),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    if founder is None:
        denied = enforce_roles(request, ("viewer", "analyst", "admin"))
        if denied:
            return denied

    target = (
        db.query(models.Founder)
        .filter(models.Founder.id == founder_id)
        .one_or_none()
    )
    if target is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "founder not found"
        )
    if not _founder_owns(founder, target):
        return error_response(
            request_id, 403, "FORBIDDEN", "founder not owned by caller"
        )

    record = latest_result_for_founder(db, target.id)
    effective = evaluate_effective_status(record)
    if record is None:
        return envelope(
            request_id=request_id,
            data={
                "founder_id": target.id,
                "status": "absent",
                "result": None,
            },
        )
    return envelope(
        request_id=request_id,
        data={
            "founder_id": target.id,
            "status": effective,
            "result": {
                "result_id": record.id,
                "provider": record.provider,
                "status": record.status,
                "screening_categories": record.screening_categories,
                "expires_at": record.expires_at.isoformat()
                if record.expires_at
                else None,
                "completed_at": record.completed_at.isoformat()
                if record.completed_at
                else None,
                "refresh_required_at": record.refresh_required_at.isoformat()
                if record.refresh_required_at
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
    """Common webhook handler -- verify signature, apply update, surface result."""
    try:
        backend = _resolve_backend(provider)
    except (
        FounderKYCBackendConfigError,
        FounderKYCBackendError,
        ValueError,
    ) as exc:
        # Provider misconfiguration on our side is a 503 -- never silently
        # accept an unverifiable webhook.
        raise KYCError(f"backend_unavailable:{exc}") from exc
    record = apply_webhook(
        db,
        backend=backend,
        raw_payload=raw,
        headers=headers,
        trace_id=trace_id,
    )
    if record is None:
        return {"status": "ignored", "result_id": None}
    return {
        "status": record.status,
        "result_id": record.id,
        "screening_categories": record.screening_categories,
    }


def _webhook_response(
    request: Request,
    db: Session,
    provider: str,
):
    raw_coro = _read_raw_body(request)
    return raw_coro, provider, db


@router.post("/webhooks/founder_kyc/persona")
async def persona_kyc_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    raw = await _read_raw_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    trace_id = headers.get("x-request-id") or f"trace_{uuid.uuid4().hex[:12]}"
    request_id = headers.get("x-request-id") or new_request_id()
    try:
        result = _handle_webhook(
            "persona", raw, headers, db, trace_id=trace_id
        )
    except KYCError as exc:
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


@router.post("/webhooks/founder_kyc/onfido")
async def onfido_kyc_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    raw = await _read_raw_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    trace_id = headers.get("x-request-id") or f"trace_{uuid.uuid4().hex[:12]}"
    request_id = headers.get("x-request-id") or new_request_id()
    try:
        result = _handle_webhook(
            "onfido", raw, headers, db, trace_id=trace_id
        )
    except KYCError as exc:
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
