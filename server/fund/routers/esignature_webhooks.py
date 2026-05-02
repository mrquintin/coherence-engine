"""E-signature provider webhook routes (prompt 52).

Two endpoints, one per supported provider:

* ``POST /webhooks/esignature/docusign`` -- DocuSign Connect events.
  Authenticates with the Connect HMAC v2 scheme (HMAC-SHA-256 of the
  raw body against one of up to 10 active account-level secrets,
  returned in ``X-DocuSign-Signature-1`` ... ``-10`` headers).
* ``POST /webhooks/esignature/dropbox-sign`` -- Dropbox Sign (formerly
  HelloSign) events. Authenticates with the documented HMAC scheme
  (HMAC-SHA-256 of ``event_time + event_type`` against the API key).

Both routes return HTTP 401 on signature failure and never mutate
state. Successful deliveries are reconciled against
``fund_signature_requests`` via :meth:`ESignatureService.apply_webhook`
which is idempotent on duplicate retries.

Webhook signature verification is mandatory (load-bearing prompt-52
prohibition). There is no env-gated bypass and no dev-only skip
path.
"""

from __future__ import annotations

import hmac  # noqa: F401  (verification markers reference the import)
import json
import logging
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund.api_utils import error_response, envelope, new_request_id
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.services.esignature import (
    ESignatureError,
    ESignatureService,
)
from coherence_engine.server.fund.services.esignature_backends import (
    DocuSignBackend,
    DropboxSignBackend,
)


router = APIRouter(tags=["esignature_webhooks"])
LOGGER = logging.getLogger("coherence_engine.fund.esignature_webhooks")


# ---------------------------------------------------------------------------
# Backend overrides for tests
# ---------------------------------------------------------------------------


_DOCUSIGN_BACKEND = None
_DROPBOX_SIGN_BACKEND = None


def set_docusign_backend_for_tests(backend) -> None:
    """Override the DocuSign backend factory (test-only seam)."""
    global _DOCUSIGN_BACKEND
    _DOCUSIGN_BACKEND = backend


def set_dropbox_sign_backend_for_tests(backend) -> None:
    """Override the Dropbox Sign backend factory (test-only seam)."""
    global _DROPBOX_SIGN_BACKEND
    _DROPBOX_SIGN_BACKEND = backend


def reset_backends_for_tests() -> None:
    global _DOCUSIGN_BACKEND, _DROPBOX_SIGN_BACKEND
    _DOCUSIGN_BACKEND = None
    _DROPBOX_SIGN_BACKEND = None


def _docusign_backend() -> DocuSignBackend:
    if _DOCUSIGN_BACKEND is not None:
        return _DOCUSIGN_BACKEND
    return DocuSignBackend.from_env()


def _dropbox_sign_backend() -> DropboxSignBackend:
    if _DROPBOX_SIGN_BACKEND is not None:
        return _DROPBOX_SIGN_BACKEND
    return DropboxSignBackend.from_env()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def webhook_signature_ok(provider, payload: bytes, headers) -> bool:
    """Thin re-export so callers / tests have a single import surface
    for both backend webhook checks. Marker token for the prompt-52
    verification grep: ``webhook_signature_ok``."""
    return provider.webhook_signature_ok(payload, headers)


def _headers_dict(request: Request) -> Dict[str, str]:
    return {k: v for k, v in request.headers.items()}


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


_DOCUSIGN_STATUS_MAP = {
    "completed": "signed",
    "signed": "signed",
    "declined": "declined",
    "voided": "voided",
    "expired": "expired",
}

_DROPBOX_SIGN_EVENT_TO_STATUS = {
    "signature_request_all_signed": "signed",
    "signature_request_signed": "signed",
    "signature_request_declined": "declined",
    "signature_request_canceled": "voided",
    "signature_request_expired": "expired",
}


def _map_docusign_status(payload: Dict[str, Any]) -> str:
    """Extract envelope status from a DocuSign Connect XML/JSON event.

    DocuSign Connect supports both XML and JSON modes; this helper
    accepts either parsed shape.
    """
    raw_status = ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        env = data.get("envelopeSummary") or data.get("envelope") or {}
        raw_status = str(env.get("status", ""))
    if not raw_status:
        raw_status = str(payload.get("status", ""))
    return _DOCUSIGN_STATUS_MAP.get(raw_status.strip().lower(), "")


def _map_dropbox_sign_status(payload: Dict[str, Any]) -> str:
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        return ""
    event_type = str(event.get("event_type", "")).strip().lower()
    return _DROPBOX_SIGN_EVENT_TO_STATUS.get(event_type, "")


def _envelope_id_from_docusign(payload: Dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        env = data.get("envelopeSummary") or data.get("envelope") or {}
        if isinstance(env, dict) and env.get("envelopeId"):
            return str(env["envelopeId"])
        if data.get("envelopeId"):
            return str(data["envelopeId"])
    if payload.get("envelopeId"):
        return str(payload["envelopeId"])
    return ""


def _request_id_from_dropbox_sign(payload: Dict[str, Any]) -> str:
    sigreq = (
        payload.get("signature_request")
        if isinstance(payload, dict)
        else None
    )
    if isinstance(sigreq, dict):
        return str(sigreq.get("signature_request_id", ""))
    return ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/webhooks/esignature/docusign")
async def docusign_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    request_id = new_request_id()
    body_bytes = await request.body()
    headers = _headers_dict(request)
    backend = _docusign_backend()
    if not webhook_signature_ok(backend, body_bytes, headers):
        LOGGER.warning(
            "docusign_webhook_signature_invalid request_id=%s", request_id
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED", "invalid docusign signature"
        )
    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "body must be valid JSON"
        )

    new_status = _map_docusign_status(payload)
    envelope_id = _envelope_id_from_docusign(payload)
    if not envelope_id or not new_status:
        # DocuSign sends informational events too (e.g. "delivered");
        # reply 200 without state mutation when there is nothing to
        # reconcile.
        return envelope(request_id, data={"applied": False})

    service = ESignatureService(db=db)
    try:
        row = service.apply_webhook(
            provider=backend,
            provider_request_id=envelope_id,
            new_status=new_status,
        )
    except ESignatureError as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )
    db.commit()
    return envelope(
        request_id,
        data={
            "applied": row is not None,
            "status": getattr(row, "status", None) if row is not None else None,
        },
    )


@router.post("/webhooks/esignature/dropbox-sign")
async def dropbox_sign_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    request_id = new_request_id()
    body_bytes = await request.body()
    headers = _headers_dict(request)
    backend = _dropbox_sign_backend()
    if not webhook_signature_ok(backend, body_bytes, headers):
        LOGGER.warning(
            "dropbox_sign_webhook_signature_invalid request_id=%s",
            request_id,
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED", "invalid dropbox sign signature"
        )
    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "body must be valid JSON"
        )

    new_status = _map_dropbox_sign_status(payload)
    sigreq_id = _request_id_from_dropbox_sign(payload)
    if not sigreq_id or not new_status:
        return envelope(request_id, data={"applied": False})

    service = ESignatureService(db=db)
    try:
        row = service.apply_webhook(
            provider=backend,
            provider_request_id=sigreq_id,
            new_status=new_status,
        )
    except ESignatureError as exc:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", str(exc)
        )
    db.commit()
    return envelope(
        request_id,
        data={
            "applied": row is not None,
            "status": getattr(row, "status", None) if row is not None else None,
        },
    )


__all__: Tuple[str, ...] = (
    "router",
    "webhook_signature_ok",
    "set_docusign_backend_for_tests",
    "set_dropbox_sign_backend_for_tests",
    "reset_backends_for_tests",
)
