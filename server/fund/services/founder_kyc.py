"""Founder KYC/AML screening service (prompt 53).

Mandatory upstream of any capital instruction. Distinct from
:mod:`accredited_verification` (prompt 26) which gates LP-side
accredited-investor verification: that flow targets *who is putting
capital in*, this flow targets *who is receiving capital out*.

The two are deliberately separate modules with separate tables
(``fund_verification_records`` vs ``fund_kyc_results``) and separate
provider-secret env vars so a leaked secret cannot cross the trust
boundary.

Status vocabulary
-----------------

* ``pending``  -- initiation succeeded; awaiting provider webhook.
* ``passed``   -- provider attests sanctions / PEP / ID / AML
  screening did not raise a hit.
* ``failed``   -- provider attests at least one screening category
  produced a hit. The operator UI MUST route to manual review --
  callers MUST NOT auto-reject the founder forever (prompt 53
  prohibition).
* ``expired``  -- was ``passed``, but ``expires_at`` has passed.
  Evaluated lazily; persisted rows are not mutated.

Refresh cadence
---------------

* Annual TTL: :data:`KYC_TTL_DAYS` (365) days from ``completed_at``.
* :data:`KYC_REFRESH_NOTICE_DAYS` (30) days before ``expires_at`` the
  daily refresh job emits a ``founder_kyc.refresh_due`` event so the
  operator UI can prompt the founder to re-verify before any new
  funding event.
* Re-screen on every funding event: the application_service caller
  is expected to ensure :func:`is_kyc_clear` immediately before
  issuing a capital instruction; the decision-policy ``kyc_clear``
  gate is the deterministic enforcement point.

Decision-policy gate
--------------------

A ``pass`` verdict downgrades to ``manual_review`` with reason code
``KYC_REQUIRED`` whenever :func:`is_kyc_clear` returns ``False``.
This is the single source of truth: callers do not branch on KYC
themselves, they thread ``kyc_passed`` into
``DecisionPolicyService.evaluate(application=...)``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Mapping, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.services.founder_kyc_backends import (
    FounderKYCBackend,
    FounderKYCBackendError,
    KYCInitiationResponse,
    KYCStatusResponse,
)


__all__ = [
    "FounderKYCBackend",
    "KYCInitiationResponse",
    "KYCStatusResponse",
    "KYCError",
    "KYCResult",
    "KYC_TTL_DAYS",
    "KYC_REFRESH_NOTICE_DAYS",
    "ALLOWED_STATUSES",
    "ALLOWED_SCREENING_CATEGORIES",
    "compute_idempotency_key",
    "initiate_kyc",
    "apply_webhook",
    "latest_result_for_founder",
    "evaluate_effective_status",
    "is_kyc_clear",
    "parse_screening_categories",
    "scan_refresh_due",
]


_LOG = logging.getLogger(__name__)


# Convenience re-export: callers that ``from founder_kyc import KYCResult``
# get the ORM class without a second import.
KYCResult = models.KYCResult


KYC_TTL_DAYS = 365
KYC_REFRESH_NOTICE_DAYS = 30

ALLOWED_STATUSES = frozenset({"pending", "passed", "failed", "expired"})
ALLOWED_SCREENING_CATEGORIES = frozenset({"sanctions", "pep", "id", "aml"})


class KYCError(Exception):
    """Raised by the founder-KYC service for non-recoverable failures."""


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def compute_idempotency_key(founder_id: str, provider: str, salt: str) -> str:
    """Stable idempotency key for a founder KYC attempt.

    ``salt`` is typically the provider's reference id (Persona inquiry
    id, Onfido check id) so two webhook deliveries for the same
    provider attempt collapse onto a single row, while a fresh
    re-screen of the same founder by the same provider gets a new
    salt and therefore a new row.
    """
    payload = f"founder-kyc|{founder_id}|{provider}|{salt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_screening_categories(raw: str) -> FrozenSet[str]:
    """Parse the comma-separated ``screening_categories`` column."""
    parts = {p.strip().lower() for p in (raw or "").split(",") if p.strip()}
    return frozenset(parts & ALLOWED_SCREENING_CATEGORIES)


def _serialize_screening_categories(values: Optional[Any]) -> str:
    if values is None:
        return "sanctions,pep,id,aml"
    if isinstance(values, str):
        items = {p.strip().lower() for p in values.split(",") if p.strip()}
    else:
        items = {str(v).strip().lower() for v in values}
    items &= ALLOWED_SCREENING_CATEGORIES
    return ",".join(sorted(items))


def _coerce_status(value: Any) -> str:
    s = str(value or "").strip().lower()
    # Provider-vocabulary translations -> internal.
    translations = {
        "clear": "passed",
        "approved": "passed",
        "verified": "passed",
        "consider": "failed",
        "rejected": "failed",
        "fail": "failed",
        "pass": "passed",
    }
    s = translations.get(s, s)
    if s not in ALLOWED_STATUSES:
        raise KYCError(f"unknown_kyc_status:{value!r}")
    return s


def initiate_kyc(
    db: Session,
    *,
    founder: models.Founder,
    backend: FounderKYCBackend,
    redirect_url: Optional[str] = None,
    screening_categories: Optional[Any] = None,
) -> models.KYCResult:
    """Start a KYC attempt with ``backend`` and persist a row.

    The row is created ``status="pending"``; the provider's webhook
    drives subsequent transitions. Caller is responsible for
    committing the session.
    """
    try:
        response = backend.initiate(founder, redirect_url=redirect_url)
    except FounderKYCBackendError as exc:
        raise KYCError(
            f"backend_initiate_failed:{backend.name}:{exc}"
        ) from exc

    provider_ref = (
        response.provider_reference or f"local_{uuid.uuid4().hex[:16]}"
    )
    idem = compute_idempotency_key(founder.id, backend.name, provider_ref)

    existing = (
        db.query(models.KYCResult)
        .filter(models.KYCResult.idempotency_key == idem)
        .one_or_none()
    )
    if existing is not None:
        return existing

    record = models.KYCResult(
        id=f"kyc_{uuid.uuid4().hex[:16]}",
        founder_id=founder.id,
        provider=backend.name,
        status="pending",
        screening_categories=_serialize_screening_categories(
            screening_categories
        ),
        evidence_uri="",
        evidence_hash="",
        provider_reference=provider_ref,
        idempotency_key=idem,
        error_code="",
        failure_reason="",
        created_at=_utc_now(),
        completed_at=None,
        expires_at=None,
        refresh_required_at=None,
    )
    db.add(record)
    db.flush()
    return record


def _emit_event(
    db: Session,
    *,
    event_type: str,
    record: models.KYCResult,
    trace_id: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append an outbox event for a KYC state transition.

    ``event_type`` is one of:

    * ``founder_kyc.updated``      -- terminal status change.
    * ``founder_kyc.refresh_due``  -- expiry within
      :data:`KYC_REFRESH_NOTICE_DAYS` days.
    """
    publisher = EventPublisher(db)
    payload: Dict[str, object] = {
        "founder_id": record.founder_id,
        "result_id": record.id,
        "provider": record.provider,
        "status": record.status,
        "screening_categories": list(
            sorted(parse_screening_categories(record.screening_categories))
        ),
        "expires_at": record.expires_at.isoformat()
        if record.expires_at
        else None,
        "refresh_required_at": record.refresh_required_at.isoformat()
        if record.refresh_required_at
        else None,
    }
    if extra:
        payload.update(extra)
    publisher.publish(
        event_type=event_type,
        producer="fund.founder_kyc",
        trace_id=trace_id or f"trace_{uuid.uuid4().hex[:12]}",
        idempotency_key=f"{event_type}:{record.id}:{record.status}",
        payload=payload,
    )


def apply_webhook(
    db: Session,
    *,
    backend: FounderKYCBackend,
    raw_payload: bytes,
    headers: Mapping[str, str],
    parsed_payload: Optional[Mapping[str, Any]] = None,
    trace_id: str = "",
) -> Optional[models.KYCResult]:
    """Verify and apply a provider KYC webhook delivery.

    Returns the updated :class:`KYCResult`, or ``None`` if the webhook
    references an unknown record (no-op; callers MUST still return
    200 to the provider so the delivery is not retried forever).

    Raises :class:`KYCError` on signature failure -- the caller
    (router) maps that to ``401`` and does NOT mutate any row.
    Per prompt 53 prohibition: signature verification is never
    bypassed, even in dry-run mode.
    """
    if not backend.webhook_signature_ok(raw_payload, headers):
        raise KYCError("webhook_signature_invalid")

    if parsed_payload is None:
        try:
            parsed_payload = json.loads(raw_payload.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KYCError(f"webhook_payload_invalid:{exc}") from exc

    if not isinstance(parsed_payload, Mapping):
        raise KYCError("webhook_payload_must_be_object")

    provider_ref = str(
        parsed_payload.get("provider_reference")
        or parsed_payload.get("inquiry_id")
        or parsed_payload.get("check_id")
        or ""
    ).strip()
    if not provider_ref:
        raise KYCError("webhook_missing_provider_reference")

    record = (
        db.query(models.KYCResult)
        .filter(models.KYCResult.provider == backend.name)
        .filter(models.KYCResult.provider_reference == provider_ref)
        .one_or_none()
    )
    if record is None:
        _LOG.warning(
            "kyc_webhook_for_unknown_record provider=%s ref=%s",
            backend.name,
            provider_ref,
        )
        return None

    new_status = _coerce_status(parsed_payload.get("status"))
    evidence_uri = str(parsed_payload.get("evidence_uri") or "")
    evidence_hash = str(parsed_payload.get("evidence_hash") or "")
    error_code = str(parsed_payload.get("error_code") or "")
    failure_reason = str(parsed_payload.get("failure_reason") or "")
    raw_categories = parsed_payload.get("screening_categories")
    if raw_categories is not None:
        new_categories = _serialize_screening_categories(raw_categories)
    else:
        new_categories = record.screening_categories

    # Replay protection: a webhook delivering the same status for an
    # already-terminal record is a no-op. We still return the row so
    # the router can surface a 200, but no field is rewritten and no
    # event is re-emitted.
    is_replay = (
        record.status == new_status
        and record.screening_categories == new_categories
        and record.status in {"passed", "failed"}
    )
    if is_replay:
        return record

    record.status = new_status
    record.screening_categories = new_categories
    if evidence_uri:
        record.evidence_uri = evidence_uri
    if evidence_hash:
        record.evidence_hash = evidence_hash
    record.error_code = error_code
    record.failure_reason = failure_reason

    if new_status == "passed":
        record.completed_at = _utc_now()
        record.expires_at = record.completed_at + timedelta(
            days=KYC_TTL_DAYS
        )
        record.refresh_required_at = record.expires_at - timedelta(
            days=KYC_REFRESH_NOTICE_DAYS
        )
    elif new_status == "failed":
        record.completed_at = _utc_now()
        record.expires_at = None
        record.refresh_required_at = None

    db.flush()
    _emit_event(
        db,
        event_type="founder_kyc.updated",
        record=record,
        trace_id=trace_id,
    )
    return record


def latest_result_for_founder(
    db: Session, founder_id: str
) -> Optional[models.KYCResult]:
    """Return the most recent KYC result, or ``None`` if absent."""
    return (
        db.query(models.KYCResult)
        .filter(models.KYCResult.founder_id == founder_id)
        .order_by(models.KYCResult.created_at.desc())
        .first()
    )


def evaluate_effective_status(
    record: Optional[models.KYCResult],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Compute effective status of a record, honoring expiry.

    Read-only -- does NOT mutate the row. A row stored as ``passed``
    whose ``expires_at`` is in the past is reported as ``expired``;
    callers wanting to persist the transition must write a fresh row
    (re-screen) rather than mutate history.
    """
    if record is None:
        return "absent"
    current = now or _utc_now()
    if record.status == "passed" and record.expires_at is not None:
        # SQLite drops the timezone on roundtrip; coerce naive values to
        # UTC so comparison works consistently across dialects.
        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if current >= expires_at:
            return "expired"
    return record.status


def is_kyc_clear(
    record: Optional[models.KYCResult],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Return True iff KYC is currently ``passed`` and unexpired.

    Single source of truth for the ``kyc_clear`` decision-policy gate.
    Callers thread the result through
    ``DecisionPolicyService.evaluate(application={"kyc_passed": ...})``.
    """
    return evaluate_effective_status(record, now=now) == "passed"


def scan_refresh_due(
    db: Session,
    *,
    now: Optional[datetime] = None,
    notice_days: int = KYC_REFRESH_NOTICE_DAYS,
    trace_id: str = "",
) -> int:
    """Daily job hook: emit ``founder_kyc.refresh_due`` for nearing-expiry rows.

    A ``passed`` row with ``expires_at`` within ``notice_days`` of
    ``now`` (and not yet expired) emits one
    ``founder_kyc.refresh_due`` outbox event. The event's
    ``idempotency_key`` is keyed on the result id + status so a daily
    re-run of the scan does not double-emit; the outbox dispatcher
    treats duplicates as no-ops.

    Returns the number of events emitted.
    """
    current = now or _utc_now()
    horizon = current + timedelta(days=notice_days)
    candidates = (
        db.query(models.KYCResult)
        .filter(models.KYCResult.status == "passed")
        .filter(models.KYCResult.expires_at.isnot(None))
        .filter(models.KYCResult.expires_at > current)
        .filter(models.KYCResult.expires_at <= horizon)
        .all()
    )
    emitted = 0
    publisher = EventPublisher(db)
    for record in candidates:
        days_remaining = max(
            0,
            int((record.expires_at - current).total_seconds() // 86400),
        )
        idem_key = f"founder_kyc.refresh_due:{record.id}:{record.expires_at.isoformat()}"
        existing = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.idempotency_key == idem_key)
            .one_or_none()
        )
        if existing is not None:
            continue
        payload: Dict[str, object] = {
            "founder_id": record.founder_id,
            "result_id": record.id,
            "provider": record.provider,
            "status": record.status,
            "expires_at": record.expires_at.isoformat(),
            "refresh_required_at": record.refresh_required_at.isoformat()
            if record.refresh_required_at
            else None,
            "days_remaining": days_remaining,
        }
        publisher.publish(
            event_type="founder_kyc.refresh_due",
            producer="fund.founder_kyc",
            trace_id=trace_id or f"trace_{uuid.uuid4().hex[:12]}",
            idempotency_key=idem_key,
            payload=payload,
        )
        emitted += 1
    if emitted:
        db.flush()
    return emitted
