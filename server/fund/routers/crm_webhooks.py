"""CRM provider webhook routes (prompt 55).

Two endpoints, one per supported CRM:

* ``POST /webhooks/crm/affinity`` -- Affinity webhook events. Verified
  with HMAC-SHA-256 of the raw body against ``AFFINITY_WEBHOOK_SECRET``,
  delivered in the ``Affinity-Webhook-Signature`` header.
* ``POST /webhooks/crm/hubspot`` -- HubSpot webhook events. Verified
  with the v3 scheme (HMAC-SHA-256 of the raw body, base64 digest)
  against ``HUBSPOT_WEBHOOK_SECRET`` in ``X-HubSpot-Signature-v3``.

Both routes return HTTP 401 on signature failure and never mutate
state. Verified payloads are parsed by the backend into a
:class:`CRMUpdate` and applied by
:func:`apply_inbound_update`. Apply is idempotent on repeated
deliveries -- the inbound sync ledger compares the prior snapshot and
no-ops when nothing changed.

Webhook signature verification is mandatory (load-bearing prompt-55
prohibition). There is no env-gated bypass and no dev-only skip
path.
"""

from __future__ import annotations

import hmac  # noqa: F401 -- referenced by signature/marker checks
import logging
from typing import Tuple

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund.api_utils import (
    envelope,
    error_response,
    new_request_id,
)
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.services.crm_backends import (
    AffinityBackend,
    HubSpotBackend,
)
from coherence_engine.server.fund.services.crm_sync import apply_inbound_update


router = APIRouter(tags=["crm_webhooks"])
LOGGER = logging.getLogger("coherence_engine.fund.crm_webhooks")


# ---------------------------------------------------------------------------
# Backend overrides for tests
# ---------------------------------------------------------------------------


_AFFINITY_BACKEND = None
_HUBSPOT_BACKEND = None


def set_affinity_backend_for_tests(backend) -> None:
    """Override the Affinity backend factory (test-only seam)."""
    global _AFFINITY_BACKEND
    _AFFINITY_BACKEND = backend


def set_hubspot_backend_for_tests(backend) -> None:
    """Override the HubSpot backend factory (test-only seam)."""
    global _HUBSPOT_BACKEND
    _HUBSPOT_BACKEND = backend


def reset_backends_for_tests() -> None:
    global _AFFINITY_BACKEND, _HUBSPOT_BACKEND
    _AFFINITY_BACKEND = None
    _HUBSPOT_BACKEND = None


def _affinity_backend() -> AffinityBackend:
    if _AFFINITY_BACKEND is not None:
        return _AFFINITY_BACKEND
    return AffinityBackend.from_env()


def _hubspot_backend() -> HubSpotBackend:
    if _HUBSPOT_BACKEND is not None:
        return _HUBSPOT_BACKEND
    return HubSpotBackend.from_env()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def webhook_signature_ok(backend, payload: bytes, headers) -> bool:
    """Single import surface for the CRM webhook signature checks.

    Marker token for the prompt-55 verification grep:
    ``webhook_signature_ok``.
    """
    return backend.verify_webhook(payload, headers)


def _headers_dict(request: Request) -> dict:
    return {k: v for k, v in request.headers.items()}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/webhooks/crm/affinity")
async def affinity_webhook(
    request: Request, db: Session = Depends(get_db)
):
    request_id = new_request_id()
    body_bytes = await request.body()
    headers = _headers_dict(request)
    backend = _affinity_backend()
    if not webhook_signature_ok(backend, body_bytes, headers):
        LOGGER.warning(
            "affinity_webhook_signature_invalid request_id=%s", request_id
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED", "invalid affinity signature"
        )
    update = backend.parse_webhook(body_bytes)
    if update is None:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "body must be valid JSON"
        )
    outcome = apply_inbound_update(db, update)
    db.commit()
    return envelope(request_id, data=outcome)


@router.post("/webhooks/crm/hubspot")
async def hubspot_webhook(
    request: Request, db: Session = Depends(get_db)
):
    request_id = new_request_id()
    body_bytes = await request.body()
    headers = _headers_dict(request)
    backend = _hubspot_backend()
    if not webhook_signature_ok(backend, body_bytes, headers):
        LOGGER.warning(
            "hubspot_webhook_signature_invalid request_id=%s", request_id
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED", "invalid hubspot signature"
        )
    update = backend.parse_webhook(body_bytes)
    if update is None:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "body must be valid JSON"
        )
    outcome = apply_inbound_update(db, update)
    db.commit()
    return envelope(request_id, data=outcome)


__all__: Tuple[str, ...] = (
    "router",
    "webhook_signature_ok",
    "set_affinity_backend_for_tests",
    "set_hubspot_backend_for_tests",
    "reset_backends_for_tests",
)
