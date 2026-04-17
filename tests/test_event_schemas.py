"""Offline tests for canonical fund pipeline event schemas and publisher validation."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

import pytest

from coherence_engine.server.fund.services.event_schemas import (
    SUPPORTED_EVENTS,
    EventValidationError,
    load_schema,
    validate_event,
)


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _base_fields(event_name: str) -> Dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_name": event_name,
        "schema_version": 1,
        "occurred_at": _iso_now(),
        "application_id": "app_01TEST",
    }


def _interview_completed_valid() -> Dict[str, Any]:
    base = _base_fields("interview_completed")
    base.update(
        {
            "session_id": "ivw_session_01",
            "transcript_ref": "db://applications/app_01TEST/transcript_text",
            "duration_s": 600,
            "asr_confidence_avg": 0.91,
        }
    )
    return base


def _argument_compiled_valid() -> Dict[str, Any]:
    base = _base_fields("argument_compiled")
    base.update(
        {
            "argument_graph_ref": "arg_graph_01",
            "n_propositions": 12,
            "n_relations": 18,
        }
    )
    return base


def _decision_issued_valid() -> Dict[str, Any]:
    base = _base_fields("decision_issued")
    base.update(
        {
            "decision": "pass",
            "cs_superiority": 0.29,
            "cs_required": 0.18,
            "decision_policy_version": "decision-policy-v1.0.0",
            "scoring_version": "scoring-v1.0.0",
        }
    )
    return base


def _founder_notified_valid() -> Dict[str, Any]:
    base = _base_fields("founder_notified")
    base.update(
        {
            "channel": "email",
            "template_id": "decision_notification_v1",
            "notification_status": "sent",
        }
    )
    return base


VALID_FIXTURES: Dict[str, Dict[str, Any]] = {
    "interview_completed": _interview_completed_valid(),
    "argument_compiled": _argument_compiled_valid(),
    "decision_issued": _decision_issued_valid(),
    "founder_notified": _founder_notified_valid(),
}


# Pairs of (event_name, required-field-dropped, forbidden-extra-field-added).
NEGATIVE_CASES: Dict[str, Tuple[str, str]] = {
    "interview_completed": ("session_id", "extra_field"),
    "argument_compiled": ("argument_graph_ref", "unexpected"),
    "decision_issued": ("cs_superiority", "weird_extra"),
    "founder_notified": ("template_id", "mystery_key"),
}


def test_supported_events_registry_exposes_four_names():
    assert set(SUPPORTED_EVENTS.keys()) == {
        "interview_completed",
        "argument_compiled",
        "decision_issued",
        "founder_notified",
    }
    for versions in SUPPORTED_EVENTS.values():
        assert "1" in versions


@pytest.mark.parametrize("event_name", sorted(VALID_FIXTURES.keys()))
def test_positive_fixture_validates(event_name: str):
    validate_event(event_name, VALID_FIXTURES[event_name])


@pytest.mark.parametrize("event_name", sorted(NEGATIVE_CASES.keys()))
def test_missing_required_field_fails(event_name: str):
    missing_key, _ = NEGATIVE_CASES[event_name]
    payload = dict(VALID_FIXTURES[event_name])
    payload.pop(missing_key)
    with pytest.raises(EventValidationError):
        validate_event(event_name, payload)


@pytest.mark.parametrize("event_name", sorted(NEGATIVE_CASES.keys()))
def test_forbidden_extra_property_fails(event_name: str):
    _, extra_key = NEGATIVE_CASES[event_name]
    payload = dict(VALID_FIXTURES[event_name])
    payload[extra_key] = "not-allowed"
    with pytest.raises(EventValidationError):
        validate_event(event_name, payload)


def test_decision_issued_required_exposes_canonical_fields():
    schema = load_schema("decision_issued")
    required = set(schema["required"])
    assert {"decision", "cs_superiority", "cs_required", "decision_policy_version"} <= required


def test_unsupported_event_name_raises():
    with pytest.raises(EventValidationError):
        validate_event("not_a_real_event", {"foo": "bar"})


# --- Publisher-level integration -------------------------------------------------


class _StubSession:
    """Minimal in-memory session supporting add/flush used by EventPublisher."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, rec: Any) -> None:
        self.added.append(rec)

    def flush(self) -> None:
        return None


def _make_publisher(strict: bool):
    from coherence_engine.server.fund.services.event_publisher import EventPublisher

    return EventPublisher(db=_StubSession(), strict_events=strict)


def _publish_invalid(publisher) -> None:
    publisher.publish(
        event_type="FounderNotified",
        producer="notification-service",
        trace_id="trc_test",
        idempotency_key="idem_test",
        payload={
            # Intentionally missing required template_id and notification_status.
            "application_id": "app_01TEST",
            "channel": "email",
        },
    )


def test_publisher_strict_mode_raises_on_invalid_event():
    publisher = _make_publisher(strict=True)
    with pytest.raises(EventValidationError):
        _publish_invalid(publisher)


def test_publisher_lenient_mode_logs_and_enqueues_invalid_event(caplog):
    publisher = _make_publisher(strict=False)
    with caplog.at_level(logging.WARNING, logger="coherence_engine.server.fund.services.event_publisher"):
        _publish_invalid(publisher)
    assert any("event_validation_failed_non_strict" in rec.message for rec in caplog.records)
    assert len(publisher.db.added) == 1
    enqueued = publisher.db.added[0]
    assert enqueued.event_type == "FounderNotified"
    # Payload preserved as submitted.
    assert "application_id" in json.loads(enqueued.payload_json)


def test_publisher_strict_mode_accepts_valid_event():
    publisher = _make_publisher(strict=True)
    result = publisher.publish(
        event_type="FounderNotified",
        producer="notification-service",
        trace_id="trc_test",
        idempotency_key="idem_test",
        payload={
            "application_id": "app_01TEST",
            "channel": "email",
            "template_id": "decision_notification_v1",
            "notification_status": "sent",
        },
    )
    assert result["event_id"]
    assert len(publisher.db.added) == 1
