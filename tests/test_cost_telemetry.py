"""Cost telemetry + budget alerts (prompt 62).

Covers:

* :func:`record_cost` derives ``unit_cost_usd`` + ``total_usd`` from
  the governed YAML pricing registry and never trusts a caller value.
* Idempotency: a second :func:`record_cost` with the same key returns
  the existing row, never a duplicate.
* :func:`check_application_budget` emits ``cost_budget_exceeded`` at
  threshold and refuses to re-fire inside the 24h cooldown.
* :func:`check_daily_budget` rolls up by UTC calendar day.
* The pricing registry refuses an unknown SKU.
* The Twilio cost path inside ``voice_intake.finalize_session``
  produces a CostEvent with the recorded session duration.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services.cost_alerts import (
    EVENT_COST_BUDGET_EXCEEDED,
    SCOPE_APPLICATION,
    SCOPE_DAILY,
    check_application_budget,
    check_daily_budget,
)
from coherence_engine.server.fund.services.cost_pricing import (
    CostPricingError,
    get_price,
    load_pricing_registry,
    reset_pricing_cache,
)
from coherence_engine.server.fund.services.cost_telemetry import (
    CostTelemetryError,
    compute_idempotency_key,
    record_cost,
    sum_application_total_usd,
    sum_daily_total_usd,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    os.environ.setdefault("COHERENCE_FUND_STRICT_EVENTS", "false")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_pricing_cache()
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_pricing_cache()


@pytest.fixture(autouse=True)
def _reset_alert_env(monkeypatch):
    # Predictable budget thresholds for the assertions below.
    monkeypatch.setenv("MAX_COST_PER_APPLICATION_USD", "1.00")
    monkeypatch.setenv("MAX_COST_PER_DAY_USD", "5.00")
    monkeypatch.setenv("COST_ALERT_COOLDOWN_HOURS", "24")
    yield


def _seed_application(db, application_id: str = "app_cost1") -> str:
    founder = models.Founder(
        id=f"fnd_{application_id}",
        full_name="Cost Founder",
        email=f"{application_id}@example.com",
        country="US",
        company_name="Cost Co",
    )
    db.add(founder)
    db.flush()
    application = models.Application(
        id=application_id,
        founder_id=founder.id,
        one_liner="we sell cost telemetry",
        requested_check_usd=100000,
        use_of_funds_summary="hire two engineers",
        preferred_channel="phone",
    )
    db.add(application)
    db.flush()
    return application_id


# ---------------------------------------------------------------------------
# Pricing registry
# ---------------------------------------------------------------------------


def test_pricing_registry_loads_and_returns_known_sku():
    registry = load_pricing_registry()
    assert "deepgram.nova-2.audio_minute" in registry
    entry = get_price("openai.text-embedding-3-large.tokens")
    assert entry.unit == "1000_tokens"
    assert entry.unit_cost_usd == pytest.approx(0.00013)


def test_pricing_registry_unknown_sku_raises():
    with pytest.raises(CostPricingError):
        get_price("definitely.not.a.real.sku")


# ---------------------------------------------------------------------------
# record_cost
# ---------------------------------------------------------------------------


def test_record_cost_computes_total_from_pricing_table():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        idem = compute_idempotency_key(
            provider="deepgram",
            sku="deepgram.nova-2.audio_minute",
            application_id=app_id,
            discriminator="call_sid_1",
        )
        recorded = record_cost(
            db,
            provider="deepgram",
            sku="deepgram.nova-2.audio_minute",
            units=10.0,
            application_id=app_id,
            idempotency_key=idem,
        )
        db.commit()
    finally:
        db.close()

    assert recorded.created is True
    # 10 minutes * $0.0043/min = $0.043
    assert recorded.event.total_usd == pytest.approx(10.0 * 0.0043, rel=1e-6)
    assert recorded.event.unit == "minute"
    assert recorded.event.application_id == "app_cost1"


def test_record_cost_idempotency_returns_existing_row_no_duplicate():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        idem = compute_idempotency_key(
            provider="openai",
            sku="openai.text-embedding-3-large.tokens",
            application_id=app_id,
            discriminator="batch-123",
        )
        first = record_cost(
            db,
            provider="openai",
            sku="openai.text-embedding-3-large.tokens",
            units=5.0,
            application_id=app_id,
            idempotency_key=idem,
        )
        second = record_cost(
            db,
            provider="openai",
            sku="openai.text-embedding-3-large.tokens",
            units=5.0,
            application_id=app_id,
            idempotency_key=idem,
        )
        db.commit()
        # Only one row, regardless of the number of calls.
        all_rows = db.query(models.CostEvent).all()
    finally:
        db.close()

    assert first.created is True
    assert second.created is False
    assert second.event.id == first.event.id
    assert len(all_rows) == 1


def test_record_cost_rejects_negative_units():
    db = SessionLocal()
    try:
        with pytest.raises(CostTelemetryError):
            record_cost(
                db,
                provider="openai",
                sku="openai.text-embedding-3-large.tokens",
                units=-1.0,
                application_id=None,
                idempotency_key="negkey",
            )
    finally:
        db.close()


def test_record_cost_unknown_sku_raises_pricing_error():
    db = SessionLocal()
    try:
        with pytest.raises(CostPricingError):
            record_cost(
                db,
                provider="madeup",
                sku="does.not.exist.anywhere",
                units=1.0,
                application_id=None,
                idempotency_key="bad-sku-key",
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Roll-ups
# ---------------------------------------------------------------------------


def test_sum_application_total_usd_across_multiple_skus():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        for i, (sku, units) in enumerate(
            [
                ("deepgram.nova-2.audio_minute", 1.0),
                ("openai.text-embedding-3-small.tokens", 4.0),
                ("twilio.voice.outbound_us", 3.0),
            ]
        ):
            record_cost(
                db,
                provider=sku.split(".")[0],
                sku=sku,
                units=units,
                application_id=app_id,
                idempotency_key=f"k{i}",
            )
        db.commit()
        total = sum_application_total_usd(db, app_id)
    finally:
        db.close()

    expected = (
        1.0 * 0.0043
        + 4.0 * 0.00002
        + 3.0 * 0.013
    )
    assert total == pytest.approx(expected, rel=1e-6)


def test_sum_daily_total_usd_filters_by_utc_day():
    today = datetime.now(tz=timezone.utc)
    yesterday = today - timedelta(days=1)
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        record_cost(
            db,
            provider="twilio",
            sku="twilio.voice.outbound_us",
            units=10.0,
            application_id=app_id,
            idempotency_key="today1",
            occurred_at=today,
        )
        record_cost(
            db,
            provider="twilio",
            sku="twilio.voice.outbound_us",
            units=10.0,
            application_id=app_id,
            idempotency_key="yest1",
            occurred_at=yesterday,
        )
        db.commit()
        today_total = sum_daily_total_usd(db, day=today)
        yest_total = sum_daily_total_usd(db, day=yesterday)
    finally:
        db.close()

    assert today_total == pytest.approx(10.0 * 0.013, rel=1e-6)
    assert yest_total == pytest.approx(10.0 * 0.013, rel=1e-6)


# ---------------------------------------------------------------------------
# Budget alerts
# ---------------------------------------------------------------------------


def _push_application_over_budget(db, app_id: str, idem_suffix: str = "") -> None:
    # 200 minutes * $0.013 = $2.60 -- well over the $1.00 cap.
    record_cost(
        db,
        provider="twilio",
        sku="twilio.voice.outbound_us",
        units=200.0,
        application_id=app_id,
        idempotency_key=f"twilio-many-{idem_suffix or app_id}",
    )


def test_check_application_budget_under_threshold_no_alert():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        record_cost(
            db,
            provider="twilio",
            sku="twilio.voice.outbound_us",
            units=1.0,
            application_id=app_id,
            idempotency_key="small1",
        )
        db.commit()
        decision = check_application_budget(db, app_id)
        db.commit()
    finally:
        db.close()

    assert decision.exceeded is False
    assert decision.alert_emitted is False


def test_check_application_budget_emits_event_at_threshold():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        _push_application_over_budget(db, app_id)
        db.commit()
        decision = check_application_budget(db, app_id)
        db.commit()
        outbox_rows = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == EVENT_COST_BUDGET_EXCEEDED)
            .all()
        )
    finally:
        db.close()

    assert decision.exceeded is True
    assert decision.alert_emitted is True
    assert decision.event_id is not None
    assert len(outbox_rows) == 1

    payload = json.loads(outbox_rows[0].payload_json)
    assert payload["scope"] == SCOPE_APPLICATION
    assert payload["scope_key"] == "app_cost1"
    assert payload["budget_usd"] == pytest.approx(1.0)
    assert payload["total_usd"] >= payload["budget_usd"]


def test_check_application_budget_cooldown_prevents_double_fire():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        _push_application_over_budget(db, app_id)
        db.commit()

        first = check_application_budget(db, app_id)
        db.commit()
        second = check_application_budget(db, app_id)
        db.commit()

        outbox_rows = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == EVENT_COST_BUDGET_EXCEEDED)
            .all()
        )
    finally:
        db.close()

    assert first.alert_emitted is True
    assert second.alert_emitted is False
    assert second.cooldown_active is True
    # Cooldown means exactly one outbox row, not two.
    assert len(outbox_rows) == 1


def test_check_daily_budget_emits_when_total_exceeds():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        # 600 minutes * $0.013 = $7.80 -- over the $5.00 daily cap.
        record_cost(
            db,
            provider="twilio",
            sku="twilio.voice.outbound_us",
            units=600.0,
            application_id=app_id,
            idempotency_key="daily-over",
        )
        db.commit()
        decision = check_daily_budget(db)
        db.commit()
        outbox_rows = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == EVENT_COST_BUDGET_EXCEEDED)
            .all()
        )
    finally:
        db.close()

    assert decision.scope == SCOPE_DAILY
    assert decision.exceeded is True
    assert decision.alert_emitted is True
    assert len(outbox_rows) == 1


def test_check_daily_budget_under_threshold_no_alert():
    db = SessionLocal()
    try:
        app_id = _seed_application(db)
        record_cost(
            db,
            provider="twilio",
            sku="twilio.voice.outbound_us",
            units=10.0,
            application_id=app_id,
            idempotency_key="daily-small",
        )
        db.commit()
        decision = check_daily_budget(db)
        db.commit()
    finally:
        db.close()

    assert decision.exceeded is False
    assert decision.alert_emitted is False


# ---------------------------------------------------------------------------
# voice_intake integration: Twilio cost recorded on session finalize
# ---------------------------------------------------------------------------


def test_voice_intake_finalize_records_twilio_cost():
    from coherence_engine.server.fund.services import voice_intake

    db = SessionLocal()
    try:
        app_id = _seed_application(db, application_id="app_voice1")
        session = models.InterviewSession(
            id="iv_voice1",
            application_id=app_id,
            channel="voice",
            locale="en-US",
            status="active",
        )
        db.add(session)
        rec = models.InterviewRecording(
            id="rec_voice1",
            application_id=app_id,
            session_id=session.id,
            topic_id="interview_opening",
            recording_uri="coh://x",
            recording_sha256="0" * 64,
            duration_seconds=120.0,  # 2 minutes
            provider_recording_sid="REC_test",
            status="recorded",
        )
        db.add(rec)
        db.flush()
        topics = (
            voice_intake.InterviewTopic(
                id="interview_opening", prompt="Tell us..."
            ),
        )
        voice_intake.finalize_session(
            db,
            session=session,
            topics=topics,
            provider_call_sid="CA_voice1",
        )
        db.commit()

        cost_rows = (
            db.query(models.CostEvent)
            .filter(models.CostEvent.application_id == app_id)
            .all()
        )
    finally:
        db.close()

    assert len(cost_rows) == 1
    row = cost_rows[0]
    assert row.provider == "twilio"
    assert row.sku == "twilio.voice.outbound_us"
    # 2 minutes * $0.013/min
    assert row.total_usd == pytest.approx(2.0 * 0.013, rel=1e-6)


def test_voice_intake_finalize_idempotent_does_not_double_charge():
    from coherence_engine.server.fund.services import voice_intake

    db = SessionLocal()
    try:
        app_id = _seed_application(db, application_id="app_voice2")
        session = models.InterviewSession(
            id="iv_voice2",
            application_id=app_id,
            channel="voice",
            locale="en-US",
            status="active",
        )
        db.add(session)
        rec = models.InterviewRecording(
            id="rec_voice2",
            application_id=app_id,
            session_id=session.id,
            topic_id="interview_opening",
            recording_uri="coh://x",
            recording_sha256="0" * 64,
            duration_seconds=60.0,
            provider_recording_sid="REC_test",
            status="recorded",
        )
        db.add(rec)
        db.flush()
        topics = (
            voice_intake.InterviewTopic(
                id="interview_opening", prompt="Tell us..."
            ),
        )
        voice_intake.finalize_session(
            db,
            session=session,
            topics=topics,
            provider_call_sid="CA_voice2",
        )
        db.commit()

        # Simulate a webhook-redeliver by replaying the cost-record
        # path with the same logical inputs through the public
        # ``record_cost`` API: the SHA-keyed idempotency means no
        # second row is written.
        from coherence_engine.server.fund.services.cost_telemetry import (
            compute_idempotency_key,
            record_cost,
        )

        idem = compute_idempotency_key(
            provider="twilio",
            sku="twilio.voice.outbound_us",
            application_id=app_id,
            discriminator="voice:CA_voice2",
        )
        record_cost(
            db,
            provider="twilio",
            sku="twilio.voice.outbound_us",
            units=1.0,
            application_id=app_id,
            idempotency_key=idem,
        )
        db.commit()

        cost_rows = (
            db.query(models.CostEvent)
            .filter(models.CostEvent.application_id == app_id)
            .all()
        )
    finally:
        db.close()

    assert len(cost_rows) == 1
