"""CRM bidirectional sync service (prompt 55).

Synchronizes :class:`~coherence_engine.server.fund.models.Founder` and
:class:`~coherence_engine.server.fund.models.Application` records to a
CRM (Affinity primary, HubSpot alternate) and reconciles partner-side
edits flowing back from the CRM webhooks.

Three flows live in this module:

1. **Outbound** -- :func:`enqueue_outbound_upsert` is called from
   :class:`ApplicationService` whenever ``Application.status`` or
   ``Decision.decision`` (the verdict) changes. It writes a
   ``crm_upsert_requested`` event to the outbox; a downstream worker
   picks up the event and calls
   :meth:`CRMBackend.upsert_founder` / :meth:`CRMBackend.upsert_application`.
2. **Inbound** -- :func:`apply_inbound_update` accepts a
   :class:`CRMUpdate` (parsed by :meth:`CRMBackend.parse_webhook`) and
   merges it onto the local view. Conflict policy is last-writer-wins
   for ``tags`` and ``notes``; the partner-side ``deal_stage`` label
   is mirrored in the sync ledger but **never** mapped onto
   ``Decision.decision`` (load-bearing prompt-55 prohibition).
3. **Reconciliation** -- :func:`reconcile_crm_deltas` runs once per
   day. It calls :meth:`CRMBackend.fetch_recent_updates` for the
   trailing 24h, compares to the local sync ledger, applies any
   missed updates via :func:`apply_inbound_update`, and emits a
   ``crm_reconciliation_completed`` event summarizing the run.

Sync ledger
-----------

Without adding a dedicated table, this module uses the existing
``fund_event_outbox`` as the durable ledger. Each successful inbound
application persists a ``crm_inbound_applied`` event whose payload
holds ``{provider, external_id, application_id, tags, notes,
deal_stage, occurred_at}``. The most recent event for a given
``(provider, external_id)`` represents the local mirror's view of CRM
state -- :func:`_local_snapshot` reads it back during reconciliation
to detect missed deltas without a join against the application table.

Conflict policy and prohibitions
--------------------------------

* ``Decision.decision`` (the verdict) is produced exclusively by
  :mod:`decision_policy`. CRM webhooks may carry a partner-side stage
  label but it is recorded in the sync ledger only -- it never writes
  to ``Decision``.
* Webhook signatures are mandatory; verification lives in
  :mod:`crm_backends` and the router. There is no "skip" path.
* A ``None`` / null on the CRM side does not delete a local field;
  empty / missing values in :class:`CRMUpdate` mean "no change" not
  "clear it". Tags/notes are merged additively when the CRM payload
  carries them; an empty list is interpreted as "no signal in this
  delivery".
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.crm_backends import (
    CRMBackend,
    CRMBackendError,
    CRMUpdate,
)
from coherence_engine.server.fund.services.event_publisher import EventPublisher


__all__ = [
    "CRMSyncService",
    "ReconciliationResult",
    "enqueue_outbound_upsert",
    "apply_inbound_update",
    "reconcile_crm_deltas",
    "CRM_OUTBOUND_EVENT",
    "CRM_INBOUND_EVENT",
    "CRM_RECONCILIATION_EVENT",
]


_LOG = logging.getLogger(__name__)


CRM_OUTBOUND_EVENT = "crm_upsert_requested"
CRM_INBOUND_EVENT = "crm_inbound_applied"
CRM_RECONCILIATION_EVENT = "crm_reconciliation_completed"


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationResult:
    """Summary of one reconciliation run.

    ``applied`` -- count of CRM deltas that were missed and have now
    been applied locally.
    ``skipped_already_applied`` -- count of deltas that matched the
    local mirror and required no change.
    ``unresolved`` -- count of deltas the service could not map to a
    local application.
    ``window_started_at`` / ``window_ended_at`` -- the trailing window
    examined (ISO-8601 Z).
    """

    applied: int
    skipped_already_applied: int
    unresolved: int
    window_started_at: str
    window_ended_at: str


# ---------------------------------------------------------------------------
# Outbound enqueue
# ---------------------------------------------------------------------------


def enqueue_outbound_upsert(
    db: Session,
    *,
    application_id: str,
    reason: str,
    trace_id: Optional[str] = None,
) -> Dict[str, str]:
    """Emit a ``crm_upsert_requested`` outbox event.

    Called by :class:`ApplicationService` whenever ``Application.status``
    or ``Decision.decision`` changes. The event is consumed by a
    downstream worker that issues the actual HTTP upsert; this module
    does NOT call the CRM backend synchronously inside the request
    path so a CRM outage cannot stall application progression.

    The event payload carries the application id plus a snapshot of
    the fields the worker will need (founder id + email, status,
    verdict, requested check). The worker re-reads the rows at send
    time, so this snapshot is informational only.
    """
    if not application_id:
        raise ValueError("application_id required")
    application = (
        db.query(models.Application)
        .filter(models.Application.id == application_id)
        .one_or_none()
    )
    if application is None:
        raise ValueError(f"application_not_found:{application_id}")
    founder = (
        db.query(models.Founder)
        .filter(models.Founder.id == application.founder_id)
        .one_or_none()
    )
    decision = (
        db.query(models.Decision)
        .filter(models.Decision.application_id == application_id)
        .one_or_none()
    )

    payload: Dict[str, Any] = {
        "application_id": application_id,
        "founder_id": application.founder_id,
        "founder_email": getattr(founder, "email", "") or "",
        "company_name": getattr(founder, "company_name", "") or "",
        "full_name": getattr(founder, "full_name", "") or "",
        "status": application.status,
        "verdict": getattr(decision, "decision", "") if decision else "",
        "requested_check_usd": int(application.requested_check_usd or 0),
        "one_liner": application.one_liner or "",
        "reason": reason,
    }
    publisher = EventPublisher(db=db, strict_events=False)
    return publisher.publish(
        event_type=CRM_OUTBOUND_EVENT,
        producer="crm_sync",
        trace_id=trace_id or str(uuid.uuid4()),
        idempotency_key=f"{application_id}:{application.status}:{payload['verdict']}:{reason}",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Inbound application
# ---------------------------------------------------------------------------


def _local_snapshot(
    db: Session, *, provider: str, external_id: str
) -> Dict[str, Any]:
    """Most-recent applied snapshot for ``(provider, external_id)``.

    Reads back the latest ``crm_inbound_applied`` outbox event whose
    payload matches the key. Returns ``{}`` when no prior application
    exists.
    """
    if not (provider and external_id):
        return {}
    rows = (
        db.query(models.EventOutbox)
        .filter(models.EventOutbox.event_type == CRM_INBOUND_EVENT)
        .order_by(models.EventOutbox.occurred_at.desc())
        .limit(200)
        .all()
    )
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if (
            str(payload.get("provider")) == provider
            and str(payload.get("external_id")) == external_id
        ):
            return payload
    return {}


def _resolve_application(
    db: Session, update: CRMUpdate
) -> Optional[models.Application]:
    """Resolve the local application a :class:`CRMUpdate` refers to.

    Tries the explicit ``application_id`` first, then ``founder_email``
    if the application id is absent. Returns ``None`` if no local
    record matches.
    """
    if update.application_id:
        app = (
            db.query(models.Application)
            .filter(models.Application.id == update.application_id)
            .one_or_none()
        )
        if app is not None:
            return app
    if update.founder_email:
        founder = (
            db.query(models.Founder)
            .filter(models.Founder.email == update.founder_email)
            .order_by(models.Founder.created_at.desc())
            .first()
        )
        if founder is not None:
            app = (
                db.query(models.Application)
                .filter(models.Application.founder_id == founder.id)
                .order_by(models.Application.created_at.desc())
                .first()
            )
            return app
    return None


def _snapshot_matches(
    snapshot: Mapping[str, Any], update: CRMUpdate
) -> bool:
    """True iff the local snapshot already mirrors ``update``."""
    return (
        list(snapshot.get("tags") or []) == list(update.tags or [])
        and list(snapshot.get("notes") or []) == list(update.notes or [])
        and str(snapshot.get("deal_stage", "")) == str(update.deal_stage or "")
    )


def apply_inbound_update(
    db: Session,
    update: CRMUpdate,
    *,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a parsed :class:`CRMUpdate` against local state.

    Conflict policy:

    * Tags / notes / deal-stage labels: **last-writer-wins**. Whatever
      the CRM webhook delivered replaces the prior mirror.
    * ``Decision.decision`` (the verdict): **never** modified. The
      partner-side stage label is recorded in the sync ledger only.
    * A null / missing field on the CRM side is treated as "no
      signal" and does NOT clear a previously-mirrored value.

    Returns a small dict describing the outcome:
    ``{"applied": bool, "application_id": str, "reason": str}``.
    """
    if not isinstance(update, CRMUpdate):
        raise TypeError("apply_inbound_update expects CRMUpdate")
    if not update.external_id:
        return {"applied": False, "application_id": "", "reason": "no_external_id"}

    application = _resolve_application(db, update)
    if application is None:
        # Record the inability to resolve so reconciliation can retry
        # tomorrow against fresher local state.
        return {
            "applied": False,
            "application_id": "",
            "reason": "unresolved_application",
        }

    snapshot = _local_snapshot(
        db, provider=update.provider, external_id=update.external_id
    )

    # Carry-forward semantics: missing tags/notes in the new payload
    # leave the previous values alone (CRM null != local clear).
    next_tags = (
        list(update.tags) if update.tags else list(snapshot.get("tags") or [])
    )
    next_notes = (
        list(update.notes)
        if update.notes
        else list(snapshot.get("notes") or [])
    )
    next_stage = (
        update.deal_stage
        if update.deal_stage
        else str(snapshot.get("deal_stage", ""))
    )

    merged = CRMUpdate(
        provider=update.provider,
        external_id=update.external_id,
        application_id=application.id,
        founder_email=update.founder_email,
        tags=tuple(next_tags),
        notes=tuple(next_notes),
        deal_stage=next_stage,
        occurred_at=update.occurred_at,
        raw=update.raw,
    )

    if _snapshot_matches(snapshot, merged):
        return {
            "applied": False,
            "application_id": application.id,
            "reason": "already_current",
        }

    payload: Dict[str, Any] = {
        "provider": merged.provider,
        "external_id": merged.external_id,
        "application_id": application.id,
        "tags": list(merged.tags),
        "notes": list(merged.notes),
        "deal_stage": merged.deal_stage,
        "occurred_at": merged.occurred_at or _iso(_utc_now()),
        # NOTE: we explicitly do NOT include any "verdict" field. The
        # CRM does not own the verdict; trying to write one here would
        # be silently dropped further down the pipeline. This tag is
        # the marker for the prompt-55 prohibition.
        "verdict_locked": True,
    }
    publisher = EventPublisher(db=db, strict_events=False)
    publisher.publish(
        event_type=CRM_INBOUND_EVENT,
        producer="crm_sync",
        trace_id=trace_id or str(uuid.uuid4()),
        idempotency_key=(
            f"{merged.provider}:{merged.external_id}:"
            f"{merged.occurred_at or _iso(_utc_now())}"
        ),
        payload=payload,
    )
    return {
        "applied": True,
        "application_id": application.id,
        "reason": "applied",
    }


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile_crm_deltas(
    db: Session,
    backend: CRMBackend,
    *,
    now: Optional[datetime] = None,
    window: timedelta = timedelta(hours=24),
    trace_id: Optional[str] = None,
) -> ReconciliationResult:
    """Daily reconciliation job.

    Pulls the trailing-window CRM deltas via
    :meth:`CRMBackend.fetch_recent_updates`, replays each against
    :func:`apply_inbound_update`, and emits a
    ``crm_reconciliation_completed`` event with the summary so
    operations can monitor for backlog.

    The function is deterministic given a deterministic backend: the
    in-tree backends return ``()`` from ``fetch_recent_updates`` so
    tests inject a stub backend whose ``fetch_recent_updates`` yields
    the fixture diff under examination.
    """
    end = (now or _utc_now()).astimezone(timezone.utc)
    start = end - window
    since_iso = _iso(start)
    end_iso = _iso(end)

    try:
        deltas: Sequence[CRMUpdate] = tuple(
            backend.fetch_recent_updates(since_iso=since_iso)
        )
    except CRMBackendError as exc:
        _LOG.warning(
            "crm_reconciliation_fetch_failed provider=%s error=%s",
            getattr(backend, "name", "?"),
            exc,
        )
        deltas = ()

    applied = 0
    skipped = 0
    unresolved = 0

    for delta in deltas:
        if not isinstance(delta, CRMUpdate):
            unresolved += 1
            continue
        outcome = apply_inbound_update(db, delta, trace_id=trace_id)
        if outcome.get("applied"):
            applied += 1
        elif outcome.get("reason") == "already_current":
            skipped += 1
        else:
            unresolved += 1

    summary_payload: Dict[str, Any] = {
        "provider": getattr(backend, "name", ""),
        "applied": applied,
        "skipped_already_applied": skipped,
        "unresolved": unresolved,
        "window_started_at": since_iso,
        "window_ended_at": end_iso,
    }
    publisher = EventPublisher(db=db, strict_events=False)
    publisher.publish(
        event_type=CRM_RECONCILIATION_EVENT,
        producer="crm_sync",
        trace_id=trace_id or str(uuid.uuid4()),
        idempotency_key=f"{summary_payload['provider']}:{end_iso}",
        payload=summary_payload,
    )
    return ReconciliationResult(
        applied=applied,
        skipped_already_applied=skipped,
        unresolved=unresolved,
        window_started_at=since_iso,
        window_ended_at=end_iso,
    )


# ---------------------------------------------------------------------------
# Service wrapper -- thin convenience around the module-level callables
# ---------------------------------------------------------------------------


@dataclass
class CRMSyncService:
    """Thin object-oriented wrapper around the module-level functions.

    Useful when the caller wants to inject a single service into a
    request handler / background worker rather than threading
    ``backend`` and ``db`` through each call site.
    """

    db: Session
    backend: CRMBackend

    def enqueue_outbound_upsert(
        self, *, application_id: str, reason: str
    ) -> Dict[str, str]:
        return enqueue_outbound_upsert(
            self.db, application_id=application_id, reason=reason
        )

    def apply_inbound_update(self, update: CRMUpdate) -> Dict[str, Any]:
        return apply_inbound_update(self.db, update)

    def reconcile(
        self, *, now: Optional[datetime] = None
    ) -> ReconciliationResult:
        return reconcile_crm_deltas(self.db, self.backend, now=now)
