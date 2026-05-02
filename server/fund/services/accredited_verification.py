"""Accredited-investor verification service (prompt 26).

This module wires together :mod:`accredited_backends` and the
``Investor`` / ``VerificationRecord`` ORM rows. It does *not* run the
provider's network calls itself — the backend adapter does — but it
owns persistence, idempotency, expiry, and the
``investor_verification_updated`` outbox event.

Decision/scoring pipeline impact: **none.** This service gates
LP-side capital intake into the fund. Founders applying *for* capital
are unaffected; nothing here is read by the application-scoring
pipeline.

Verification status vocabulary
------------------------------

* ``pending``   — initiation succeeded; awaiting provider webhook.
* ``verified``  — provider attests the investor is accredited under
  the recorded ``method`` (Rule 501 path).
* ``rejected``  — provider attests the investor is NOT accredited.
* ``expired``   — was ``verified``, but ``expires_at`` has passed.

Methods (SEC Rule 501 paths)
----------------------------

* ``income``                       — $200k/$300k income test.
* ``net_worth``                    — $1M net worth excluding primary residence.
* ``professional_certification``   — Series 7/65/82 etc.
* ``self_certified``               — operator-attested only; lower trust.

Expiry
------

Verified records expire :data:`VERIFICATION_TTL_DAYS` (90) days from
``completed_at`` per the SEC re-verification convention. Expiry is
*evaluated lazily* — the service does not run a scheduler; reads
recompute the effective status based on ``expires_at`` and surface
``expired`` to the caller without mutating the row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.accredited_backends import (
    AccreditedBackend,
    AccreditedBackendError,
    InitiationResponse,
    StatusResponse,
)
from coherence_engine.server.fund.services.event_publisher import EventPublisher


__all__ = [
    "AccreditedBackend",
    "InitiationResponse",
    "StatusResponse",
    "VerificationError",
    "VerificationRecord",
    "VERIFICATION_TTL_DAYS",
    "ALLOWED_METHODS",
    "ALLOWED_STATUSES",
    "compute_idempotency_key",
    "initiate_verification",
    "apply_webhook",
    "latest_record_for_investor",
    "evaluate_effective_status",
]


_LOG = logging.getLogger(__name__)


# Convenience re-export so callers that ``from
# accredited_verification import VerificationRecord`` get the ORM class
# without a second import.
VerificationRecord = models.VerificationRecord


VERIFICATION_TTL_DAYS = 90

ALLOWED_METHODS = frozenset(
    {"income", "net_worth", "professional_certification", "self_certified"}
)
ALLOWED_STATUSES = frozenset({"pending", "verified", "rejected", "expired"})


class VerificationError(Exception):
    """Raised by the verification service for non-recoverable failures."""


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def compute_idempotency_key(investor_id: str, provider: str, salt: str) -> str:
    """Stable idempotency key for a verification attempt.

    ``salt`` is typically the provider's reference id (Persona inquiry
    id, Onfido check id) so two webhook deliveries for the same
    provider attempt collapse onto a single row, while a fresh
    re-verification of the same investor by the same provider gets a
    new salt and therefore a new row.
    """
    payload = f"{investor_id}|{provider}|{salt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def initiate_verification(
    db: Session,
    *,
    investor: models.Investor,
    backend: AccreditedBackend,
    redirect_url: Optional[str] = None,
) -> models.VerificationRecord:
    """Start a verification attempt with ``backend`` and persist a row.

    The row is created in ``status="pending"``; the provider's webhook
    drives subsequent transitions. Caller is responsible for committing
    the session.
    """
    try:
        response = backend.initiate(investor, redirect_url=redirect_url)
    except AccreditedBackendError as exc:
        raise VerificationError(
            f"backend_initiate_failed:{backend.name}:{exc}"
        ) from exc

    provider_ref = response.provider_reference or f"local_{uuid.uuid4().hex[:16]}"
    idem = compute_idempotency_key(investor.id, backend.name, provider_ref)

    existing = (
        db.query(models.VerificationRecord)
        .filter(models.VerificationRecord.idempotency_key == idem)
        .one_or_none()
    )
    if existing is not None:
        return existing

    record = models.VerificationRecord(
        id=f"vrec_{uuid.uuid4().hex[:16]}",
        investor_id=investor.id,
        provider=backend.name,
        method="self_certified",
        status="pending",
        evidence_uri="",
        evidence_hash="",
        provider_reference=provider_ref,
        idempotency_key=idem,
        error_code="",
        created_at=_utc_now(),
        completed_at=None,
        expires_at=None,
    )
    db.add(record)
    db.flush()
    return record


def _coerce_method(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s not in ALLOWED_METHODS:
        return "self_certified"
    return s


def _coerce_status(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s == "fail":
        s = "rejected"
    if s == "pass":
        s = "verified"
    if s not in ALLOWED_STATUSES:
        raise VerificationError(f"unknown_verification_status:{value!r}")
    return s


def _emit_event(
    db: Session,
    *,
    investor: models.Investor,
    record: models.VerificationRecord,
    trace_id: str,
) -> None:
    """Append an ``investor_verification_updated`` event to the outbox.

    The event is *not* validated against a JSON Schema (none registered
    for this event type at prompt 26); the publisher's strict-events
    flag falls through silently for unknown event names, so we emit a
    well-formed payload and let downstream consumers schema-pin it
    when the LP intake pipeline lands.
    """
    publisher = EventPublisher(db)
    payload: Dict[str, object] = {
        "investor_id": investor.id,
        "record_id": record.id,
        "provider": record.provider,
        "status": record.status,
        "method": record.method,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
    }
    publisher.publish(
        event_type="investor_verification_updated",
        producer="fund.accredited_verification",
        trace_id=trace_id or f"trace_{uuid.uuid4().hex[:12]}",
        idempotency_key=f"ivu:{record.id}:{record.status}",
        payload=payload,
    )


def apply_webhook(
    db: Session,
    *,
    backend: AccreditedBackend,
    raw_payload: bytes,
    headers: Mapping[str, str],
    parsed_payload: Optional[Mapping[str, Any]] = None,
    trace_id: str = "",
) -> Optional[models.VerificationRecord]:
    """Verify and apply a provider webhook delivery.

    Returns the updated :class:`VerificationRecord`, or ``None`` if the
    webhook references an unknown record (no-op; callers MUST still
    return 200 to the provider so the delivery is not retried
    forever).

    Raises :class:`VerificationError` on signature failure — the
    caller (router) maps that to ``401`` and does NOT mutate any row.
    Per prompt 26 prohibition: signature verification is never
    bypassed, even in dry-run mode.
    """
    if not backend.webhook_signature_ok(raw_payload, headers):
        raise VerificationError("webhook_signature_invalid")

    if parsed_payload is None:
        try:
            parsed_payload = json.loads(raw_payload.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"webhook_payload_invalid:{exc}") from exc

    if not isinstance(parsed_payload, Mapping):
        raise VerificationError("webhook_payload_must_be_object")

    provider_ref = str(
        parsed_payload.get("provider_reference")
        or parsed_payload.get("inquiry_id")
        or parsed_payload.get("check_id")
        or ""
    ).strip()
    if not provider_ref:
        raise VerificationError("webhook_missing_provider_reference")

    record = (
        db.query(models.VerificationRecord)
        .filter(models.VerificationRecord.provider == backend.name)
        .filter(models.VerificationRecord.provider_reference == provider_ref)
        .one_or_none()
    )
    if record is None:
        _LOG.warning(
            "webhook_for_unknown_record provider=%s ref=%s",
            backend.name,
            provider_ref,
        )
        return None

    new_status = _coerce_status(parsed_payload.get("status"))
    new_method = _coerce_method(parsed_payload.get("method"))
    evidence_uri = str(parsed_payload.get("evidence_uri") or "")
    evidence_hash = str(parsed_payload.get("evidence_hash") or "")
    error_code = str(parsed_payload.get("error_code") or "")

    # Replay protection: a webhook delivering the same status+method
    # for an already-terminal record is a no-op. We still return the
    # row so the router can surface a 200, but no event is re-emitted
    # and no field is rewritten.
    is_replay = (
        record.status == new_status
        and record.method == new_method
        and (record.status in {"verified", "rejected"})
    )
    if is_replay:
        return record

    record.status = new_status
    record.method = new_method
    if evidence_uri:
        record.evidence_uri = evidence_uri
    if evidence_hash:
        record.evidence_hash = evidence_hash
    record.error_code = error_code

    if new_status == "verified":
        record.completed_at = _utc_now()
        record.expires_at = record.completed_at + timedelta(
            days=VERIFICATION_TTL_DAYS
        )
    elif new_status == "rejected":
        record.completed_at = _utc_now()
        record.expires_at = None

    investor = (
        db.query(models.Investor)
        .filter(models.Investor.id == record.investor_id)
        .one()
    )
    if new_status == "verified":
        investor.status = "verified"
    elif new_status == "rejected":
        investor.status = "rejected"

    db.flush()
    _emit_event(db, investor=investor, record=record, trace_id=trace_id)
    return record


def latest_record_for_investor(
    db: Session, investor_id: str
) -> Optional[models.VerificationRecord]:
    """Return the most recent verification record, or ``None`` if absent."""
    return (
        db.query(models.VerificationRecord)
        .filter(models.VerificationRecord.investor_id == investor_id)
        .order_by(models.VerificationRecord.created_at.desc())
        .first()
    )


def evaluate_effective_status(
    record: Optional[models.VerificationRecord],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Compute the effective status of a record, honoring expiry.

    Read-only — does NOT mutate the row. A row stored as ``verified``
    whose ``expires_at`` is in the past is reported as ``expired``;
    callers wanting to persist the transition need to write a fresh
    row (re-verification) rather than mutate history.
    """
    if record is None:
        return "absent"
    current = now or _utc_now()
    if record.status == "verified" and record.expires_at is not None:
        if current >= record.expires_at:
            return "expired"
    return record.status
