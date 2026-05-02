"""Event publisher with JSON Schema validation and outbox persistence."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.event_schemas import (
    SUPPORTED_EVENTS,
    EventValidationError,
    validate_event,
)


_LOG = logging.getLogger(__name__)


EVENT_TYPE_TO_NAME: Dict[str, str] = {
    "InterviewCompleted": "interview_completed",
    "ArgumentCompiled": "argument_compiled",
    "DecisionIssued": "decision_issued",
    "FounderNotified": "founder_notified",
}


# ---------------------------------------------------------------------------
# Scoring-mode vocabulary (prompt 12).
#
# Events carry an optional ``mode`` field so the outbox dispatcher and any
# downstream consumer can distinguish production ("enforce") decisions from
# shadow-mode replays that ran through scoring + artifact generation but
# suppressed founder/partner notification side effects. The default remains
# "enforce" so events emitted by pre-prompt-12 callers stay backward-
# compatible; the ``decision_issued.v1.json`` schema declares ``mode`` as
# optional with the same default.
# ---------------------------------------------------------------------------

SCORING_MODE_ENFORCE = "enforce"
SCORING_MODE_SHADOW = "shadow"
_ALLOWED_SCORING_MODES = frozenset({SCORING_MODE_ENFORCE, SCORING_MODE_SHADOW})


def tag_payload_with_mode(
    payload: Dict[str, object],
    mode: str,
) -> Dict[str, object]:
    """Return a shallow copy of ``payload`` with ``"mode": <mode>`` attached.

    Raises ``ValueError`` on any value outside ``{enforce, shadow}`` so
    that a typo at the call site fails loudly rather than silently
    bypassing the shadow-mode suppression logic in
    ``ApplicationService``.
    """
    if mode not in _ALLOWED_SCORING_MODES:
        raise ValueError(f"invalid_scoring_mode:{mode!r}")
    out: Dict[str, object] = dict(payload)
    out["mode"] = mode
    return out


def is_shadow_event_payload(payload: Dict[str, object]) -> bool:
    """Return True iff the payload was tagged with ``mode == "shadow"``."""
    return str(payload.get("mode", SCORING_MODE_ENFORCE)) == SCORING_MODE_SHADOW


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _env_strict_default() -> bool:
    raw = os.getenv("COHERENCE_FUND_STRICT_EVENTS", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class EventPublisher:
    """Validates and stores events in the outbox table."""

    def __init__(self, db: Session, strict_events: Optional[bool] = None):
        self.db = db
        self.strict_events = _env_strict_default() if strict_events is None else bool(strict_events)

    @staticmethod
    def _resolve_event_name(event_type: str) -> Optional[str]:
        if event_type in SUPPORTED_EVENTS:
            return event_type
        return EVENT_TYPE_TO_NAME.get(event_type)

    def _build_event_object(
        self,
        event_id: str,
        event_name: str,
        occurred_at: datetime,
        payload: Dict[str, object],
    ) -> Dict[str, object]:
        merged: Dict[str, object] = {
            "event_id": event_id,
            "event_name": event_name,
            "schema_version": 1,
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        }
        for key, value in payload.items():
            merged.setdefault(key, value)
        return merged

    def _validate(
        self,
        event_type: str,
        event_id: str,
        occurred_at: datetime,
        payload: Dict[str, object],
    ) -> None:
        event_name = self._resolve_event_name(event_type)
        if event_name is None:
            return
        event_object = self._build_event_object(event_id, event_name, occurred_at, payload)
        try:
            validate_event(event_name, event_object)
        except EventValidationError as exc:
            if self.strict_events:
                raise
            _LOG.warning(
                "event_validation_failed_non_strict event_type=%s event_id=%s error=%s",
                event_type,
                event_id,
                exc,
            )

    def publish(
        self,
        event_type: str,
        producer: str,
        trace_id: str,
        idempotency_key: str,
        payload: Dict[str, object],
        event_version: str = "1.0.0",
    ) -> Dict[str, str]:
        event_id = str(uuid.uuid4())
        occurred_at = _utc_now()
        self._validate(event_type, event_id, occurred_at, payload)
        rec = models.EventOutbox(
            id=event_id,
            event_type=event_type,
            event_version=event_version,
            producer=producer,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            payload_json=json.dumps(payload),
            status="pending",
            occurred_at=occurred_at,
        )
        self.db.add(rec)
        self.db.flush()
        return {"event_id": event_id}

    def publish_decision_policy_flag_changed(
        self,
        audit_row: Dict[str, object],
        *,
        producer: str = "feature_flags",
        trace_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Persist a ``decision_policy_flag_changed.v1`` event to the outbox.

        Used by :class:`FeatureFlags.set_restricted` when a DB-backed
        publisher is available. Validates the payload against
        ``server/fund/schemas/events/feature_flag_changed.v1.json`` (when
        ``jsonschema`` is installed) before writing the outbox row.
        """
        for required in ("audit_id", "key", "flag_type", "old_value", "new_value", "actor"):
            if required not in audit_row:
                raise ValueError(f"missing_field_in_audit_row:{required}")
        payload: Dict[str, object] = {
            "key": audit_row["key"],
            "flag_type": audit_row["flag_type"],
            "restricted": True,
            "old_value": audit_row["old_value"],
            "new_value": audit_row["new_value"],
            "actor": audit_row["actor"],
            "source": str(audit_row.get("source", "cli")),
            "audit_id": str(audit_row["audit_id"]),
        }
        reason = audit_row.get("reason")
        if reason:
            payload["reason"] = str(reason)
        self._validate_external_schema("decision_policy_flag_changed", payload)
        return self.publish(
            event_type="decision_policy_flag_changed",
            producer=producer,
            trace_id=trace_id or str(uuid.uuid4()),
            idempotency_key=str(audit_row["audit_id"]),
            payload=payload,
        )

    def _validate_external_schema(
        self,
        event_name: str,
        payload: Dict[str, object],
    ) -> None:
        """Validate ``payload`` against a schema not registered in SUPPORTED_EVENTS.

        Allows new event types whose schema lives next to the others
        without requiring a same-day edit to ``event_schemas.py``. The
        call is best-effort: if ``jsonschema`` is missing or the schema
        file is absent, validation is skipped (the same fallback the
        registered events use).
        """
        try:
            from jsonschema import Draft202012Validator  # type: ignore
        except ImportError:  # pragma: no cover - falls back like event_schemas
            return
        from pathlib import Path as _Path
        schema_path = (
            _Path(__file__).resolve().parent.parent
            / "schemas"
            / "events"
            / f"{event_name}.v1.json"
        )
        if not schema_path.exists():
            return
        with schema_path.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
        envelope = self._build_event_object(
            event_id=str(uuid.uuid4()),
            event_name=event_name,
            occurred_at=_utc_now(),
            payload=payload,
        )
        errors = sorted(
            Draft202012Validator(schema).iter_errors(envelope),
            key=lambda e: list(e.absolute_path),
        )
        if errors and self.strict_events:
            first = errors[0]
            path = ".".join(str(p) for p in first.absolute_path) or "<root>"
            from coherence_engine.server.fund.services.event_schemas import (
                EventValidationError,
            )
            raise EventValidationError(
                f"{event_name} v1 payload invalid at {path}: {first.message}"
            )
