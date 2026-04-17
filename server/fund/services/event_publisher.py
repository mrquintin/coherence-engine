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
