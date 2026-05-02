"""Capital deployment routes (prompt 51).

Three operator-facing endpoints implement the prepare / approve /
execute lifecycle, plus a Stripe webhook for funding-status callbacks.

Authorization
-------------

* ``prepare``  -- ``partner`` or ``admin`` role.
* ``approve``  -- ``treasurer`` role.
* ``execute``  -- ``treasurer`` role; the service layer enforces
  dual approval for amounts at or above
  :data:`~capital_deployment.DUAL_APPROVAL_THRESHOLD_USD`.
* ``stripe_webhook`` -- HMAC signature on the raw body is the only
  authentication; the API-key middleware skips
  ``/api/v1/webhooks/...`` paths the same way it does for other
  provider webhooks.

Non-autonomy invariant
----------------------

The router is the public surface of the non-autonomy contract: there
is no endpoint that *both* approves and executes in a single call,
and ``execute`` always returns 403 if there is no prior
:class:`~models.TreasurerApproval` row.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import (
    envelope,
    error_response,
    new_request_id,
)
from coherence_engine.server.fund.database import SessionLocal, get_db
from coherence_engine.server.fund.repositories.api_key_repository import (
    ApiKeyRepository,
)
from coherence_engine.server.fund.security import audit_log, enforce_roles
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.capital_backends import (
    CapitalBackendConfigError,
    CapitalBackendError,
    backend_for_method,
    verify_stripe_webhook_signature,
)
from coherence_engine.server.fund.services.capital_deployment import (
    CapitalDeployment,
    CapitalDeploymentError,
    InstructionStateError,
)


router = APIRouter(tags=["capital"])

LOGGER = logging.getLogger("coherence_engine.fund.capital")


# ---------------------------------------------------------------------------
# Auth glue -- mirrors partner_api._attach_principal. Capital paths are
# not in :func:`_is_fund_path`, so :class:`FundSecurityMiddleware` does
# not stamp ``request.state.principal`` for us. Pull a token off the
# request, verify via the v2 :class:`ApiKeyService`, then defer to
# :func:`enforce_roles` for the role gate.
# ---------------------------------------------------------------------------


def _attach_principal(request: Request) -> None:
    if getattr(request.state, "principal", None):
        return
    token = None
    raw = request.headers.get("x-api-key")
    if raw:
        token = raw.strip()
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        return
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        verification = svc.verify_token(repo, token)
        if not verification.get("ok"):
            db.rollback()
            return
        request.state.principal = {
            "auth_type": "api_key_db",
            "token_fingerprint": verification["fingerprint"],
            "fingerprint": verification["fingerprint"],
            "role": verification["role"],
            "key_id": verification["key_id"],
        }
        db.commit()
    finally:
        db.close()


def _gate(request: Request, allowed_roles) -> JSONResponse | None:
    _attach_principal(request)
    principal = getattr(request.state, "principal", None)
    if not principal:
        request_id = request.headers.get("x-request-id") or new_request_id()
        return error_response(
            request_id, 401, "UNAUTHORIZED", "missing or invalid API token"
        )
    return enforce_roles(request, allowed_roles)


# ---------------------------------------------------------------------------
# Backend factory injection -- parallels investor_verification's seam.
# ---------------------------------------------------------------------------

_BACKEND_FACTORY = backend_for_method


def set_backend_factory_for_tests(factory) -> None:
    """Override the backend factory (test-only seam)."""
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = factory


def reset_backend_factory_for_tests() -> None:
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = backend_for_method


def _resolve_backend(method: str):
    return _BACKEND_FACTORY(method)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None) or {}
    fingerprint = principal.get("fingerprint")
    role = principal.get("role")
    if fingerprint:
        return f"{role or 'principal'}:{fingerprint}"
    return str(role or "anonymous")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/capital/instructions:prepare", status_code=201)
def prepare_instruction(
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()

    denied = _gate(request, ("partner", "admin", "treasurer"))
    if denied:
        return denied

    application_id = str(body.get("application_id") or "").strip()
    founder_id = str(body.get("founder_id") or "").strip()
    method = str(body.get("preparation_method") or "").strip().lower()
    target_account_ref = str(body.get("target_account_ref") or "").strip()
    currency = str(body.get("currency") or "USD").strip().upper()
    try:
        amount_usd = int(body.get("amount_usd"))
    except (TypeError, ValueError):
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "amount_usd must be an integer"
        )

    if not application_id or not founder_id:
        return error_response(
            request_id, 400, "VALIDATION_ERROR",
            "application_id and founder_id required",
        )
    if method not in {"stripe", "bank_transfer"}:
        return error_response(
            request_id, 400, "VALIDATION_ERROR",
            "preparation_method must be one of stripe|bank_transfer",
        )
    if not target_account_ref:
        return error_response(
            request_id, 400, "VALIDATION_ERROR",
            "target_account_ref required (provider token)",
        )
    if amount_usd <= 0:
        return error_response(
            request_id, 400, "VALIDATION_ERROR",
            "amount_usd must be positive",
        )

    try:
        backend = _resolve_backend(method)
    except CapitalBackendConfigError as exc:
        return error_response(
            request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
        )
    except (ValueError, CapitalBackendError) as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )

    service = CapitalDeployment(db)
    try:
        instruction = service.prepare(
            backend=backend,
            application_id=application_id,
            founder_id=founder_id,
            amount_usd=amount_usd,
            currency=currency,
            target_account_ref=target_account_ref,
            preparation_method=method,
            prepared_by=_principal_id(request),
            idempotency_key=idempotency_key,
            trace_id=request_id,
        )
    except CapitalDeploymentError as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )

    db.commit()
    audit_log(
        "capital_instruction_prepare",
        request,
        "allowed",
        {
            "instruction_id": instruction.id,
            "application_id": instruction.application_id,
            "amount_usd": instruction.amount_usd,
            "method": instruction.preparation_method,
        },
    )
    return envelope(
        request_id=request_id,
        data=_serialize_instruction(instruction),
    )


@router.post("/capital/instructions/{instruction_id}:approve")
def approve_instruction(
    instruction_id: str = Path(...),
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()

    denied = _gate(request, ("treasurer", "admin"))
    if denied:
        return denied

    instruction = (
        db.query(models.InvestmentInstruction)
        .filter(models.InvestmentInstruction.id == instruction_id)
        .one_or_none()
    )
    if instruction is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "instruction not found"
        )
    if instruction.status not in {"prepared", "approved"}:
        return error_response(
            request_id, 409, "CONFLICT",
            f"cannot approve instruction in status {instruction.status}",
        )

    treasurer_id = _principal_id(request)
    note = str(body.get("note") or "")

    service = CapitalDeployment(db)
    try:
        approval = service.approve(
            instruction=instruction,
            treasurer_id=treasurer_id,
            note=note,
        )
    except CapitalDeploymentError as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )

    db.commit()
    audit_log(
        "capital_instruction_approve",
        request,
        "allowed",
        {
            "instruction_id": instruction.id,
            "treasurer_id": treasurer_id,
            "approval_id": approval.id,
        },
    )
    return envelope(
        request_id=request_id,
        data={
            "approval_id": approval.id,
            "instruction": _serialize_instruction(instruction),
        },
    )


@router.post("/capital/instructions/{instruction_id}:execute")
def execute_instruction(
    instruction_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()

    denied = _gate(request, ("treasurer", "admin"))
    if denied:
        return denied

    instruction = (
        db.query(models.InvestmentInstruction)
        .filter(models.InvestmentInstruction.id == instruction_id)
        .one_or_none()
    )
    if instruction is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "instruction not found"
        )
    if instruction.status != "approved":
        # The non-autonomy invariant: execute is forbidden without an
        # approve. Surface 403 (not 409) so the operator sees this as
        # an authorization failure rather than a transient conflict.
        return error_response(
            request_id, 403, "FORBIDDEN",
            "execute requires prior treasurer approval",
        )

    try:
        backend = _resolve_backend(instruction.preparation_method)
    except CapitalBackendConfigError as exc:
        return error_response(
            request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
        )
    except (ValueError, CapitalBackendError) as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )

    treasurer_id = _principal_id(request)
    service = CapitalDeployment(db)
    try:
        instruction = service.execute(
            backend=backend,
            instruction=instruction,
            treasurer_id=treasurer_id,
            trace_id=request_id,
        )
    except InstructionStateError as exc:
        msg = str(exc)
        # Dual-approval shortfall is the same family as missing approval --
        # surface as 403 so the operator UI can tell them which gate
        # failed.
        if "dual" in msg or "approval" in msg:
            return error_response(request_id, 403, "FORBIDDEN", msg)
        return error_response(request_id, 409, "CONFLICT", msg)
    except CapitalDeploymentError as exc:
        return error_response(
            request_id, 502, "PROVIDER_ERROR", str(exc)
        )

    db.commit()
    audit_log(
        "capital_instruction_execute",
        request,
        "allowed",
        {
            "instruction_id": instruction.id,
            "treasurer_id": treasurer_id,
            "amount_usd": instruction.amount_usd,
        },
    )
    return envelope(
        request_id=request_id,
        data=_serialize_instruction(instruction),
    )


# ---------------------------------------------------------------------------
# Stripe webhook -- funding status callbacks
# ---------------------------------------------------------------------------


async def _read_raw_body(request: Request) -> bytes:
    return await request.body()


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    raw = await _read_raw_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    request_id = headers.get("x-request-id") or new_request_id()

    secret = _stripe_webhook_secret()
    sig = headers.get("stripe-signature", "")
    if not verify_stripe_webhook_signature(secret, raw, sig):
        return error_response(
            request_id, 401, "UNAUTHORIZED", "invalid stripe webhook signature"
        )

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "malformed webhook payload"
        )

    intent_ref = (
        payload.get("data", {})
        .get("object", {})
        .get("id")
    ) or payload.get("intent_id") or ""
    new_status = payload.get("type", "")

    if not intent_ref:
        return envelope(request_id=request_id, data={"status": "ignored"})

    instruction = (
        db.query(models.InvestmentInstruction)
        .filter(models.InvestmentInstruction.provider_intent_ref == intent_ref)
        .one_or_none()
    )
    if instruction is None:
        return envelope(request_id=request_id, data={"status": "ignored"})

    if new_status in {"transfer.failed", "payment_intent.failed"}:
        instruction.status = "failed"
        instruction.error_code = (
            payload.get("data", {})
            .get("object", {})
            .get("failure_code", "")
        )[:64] or "stripe_failed"
        db.commit()
    elif new_status in {"transfer.paid", "payment_intent.succeeded"}:
        # Successful confirmation does not re-emit the funded event;
        # the event was emitted at execute time. We only persist the
        # confirmation acknowledgement.
        if instruction.status not in {"sent", "failed"}:
            instruction.status = "sent"
        db.commit()

    return envelope(
        request_id=request_id,
        data={
            "status": "accepted",
            "instruction_id": instruction.id,
            "instruction_status": instruction.status,
        },
    )


def _stripe_webhook_secret() -> str:
    import os as _os
    return _os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_instruction(
    instruction: models.InvestmentInstruction,
) -> Dict[str, Any]:
    return {
        "id": instruction.id,
        "application_id": instruction.application_id,
        "founder_id": instruction.founder_id,
        "amount_usd": int(instruction.amount_usd),
        "currency": instruction.currency,
        "preparation_method": instruction.preparation_method,
        "status": instruction.status,
        "target_account_ref": instruction.target_account_ref,
        "provider_intent_ref": instruction.provider_intent_ref,
        "prepared_by": instruction.prepared_by,
        "treasurer_id": instruction.treasurer_id,
        "prepared_at": instruction.prepared_at.isoformat()
        if instruction.prepared_at
        else None,
        "approved_at": instruction.approved_at.isoformat()
        if instruction.approved_at
        else None,
        "sent_at": instruction.sent_at.isoformat()
        if instruction.sent_at
        else None,
    }
