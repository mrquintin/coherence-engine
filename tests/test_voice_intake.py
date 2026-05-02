"""Voice-intake service tests (prompt 38).

Covers:

* ``load_topics`` returns deterministic ordering from the prompt registry.
* ``render_initial_twiml`` matches a pinned snapshot — if the rendering
  changes, the snapshot must change deliberately.
* ``start_call`` mints an :class:`InterviewSession` row and calls the
  injected fake Twilio client (no paid API calls).
* ``store_recording`` writes through the object-storage adapter and
  records a ``InterviewRecording`` with a SHA-256.
* ``finalize_session`` emits exactly one ``interview_session_completed``
  outbox event and is idempotent on a second invocation.
"""

from __future__ import annotations

import json
import os

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services import voice_intake
from coherence_engine.server.fund.services.storage_backends import (
    LocalFilesystemBackend,
)
from coherence_engine.server.fund.services.twilio_adapter import (
    TwilioCall,
    set_twilio_client_for_tests,
    reset_twilio_client_for_tests,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _FakeTwilio:
    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.fetched: dict[str, bytes] = {}

    def place_call(self, *, to: str, from_: str, twiml_url: str, status_callback_url: str) -> TwilioCall:
        self.placed.append(
            {
                "to": to,
                "from_": from_,
                "twiml_url": twiml_url,
                "status_callback_url": status_callback_url,
            }
        )
        return TwilioCall(sid="CA-fake-sid", status="queued", to=to, from_=from_)

    def fetch_recording(self, recording_sid: str) -> bytes:
        return self.fetched.get(recording_sid, b"audio-bytes")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="default")
    object_storage.set_object_storage(backend)
    fake = _FakeTwilio()
    set_twilio_client_for_tests(fake)

    yield fake

    reset_twilio_client_for_tests()
    object_storage.reset_object_storage()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _seed_application() -> tuple[str, str]:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="f_voice_test",
            full_name="Test Founder",
            email="t@example.com",
            company_name="Acme",
            country="US",
        )
        app = models.Application(
            id="app_voice_test",
            founder_id=founder.id,
            one_liner="Acme builds widgets.",
            requested_check_usd=250000,
            use_of_funds_summary="hire",
            preferred_channel="voice",
            domain_primary="market_economics",
        )
        db.add(founder)
        db.add(app)
        db.commit()
        return founder.id, app.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Topic loading
# ---------------------------------------------------------------------------


def test_load_topics_returns_deterministic_order():
    a = voice_intake.load_topics()
    b = voice_intake.load_topics()
    assert a == b
    assert len(a) >= 2
    assert a[0].id == "interview_opening"
    assert a[1].id == "self_critique"
    for t in a:
        assert t.prompt  # human-readable line


# ---------------------------------------------------------------------------
# TwiML pinned snapshot
# ---------------------------------------------------------------------------


_EXPECTED_INITIAL_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<Response>"
    "<Say>Hello, this is the Coherence Engine founder interview line. "
    "We will record short answers to a small number of topics. "
    "Please speak clearly after the tone.</Say>"
    "<Say>Tell us about the problem you are solving and your proposed solution mechanism.</Say>"
    '<Record action="https://example.test/webhooks/twilio/recording?session_id=ivw_voice_X&amp;topic_id=interview_opening" '
    'maxLength="180" '
    'recordingStatusCallback="https://example.test/webhooks/twilio/recording?session_id=ivw_voice_X&amp;topic_id=interview_opening" '
    'recordingStatusCallbackMethod="POST" '
    'finishOnKey="#" '
    'playBeep="true" '
    'trim="trim-silence" '
    'recordingTrack="inbound" '
    'recordingChannels="mono" '
    'recordingFormat="wav" />'
    "</Response>"
)


def test_render_initial_twiml_matches_snapshot():
    topics = voice_intake.load_topics()
    twiml = voice_intake.render_initial_twiml(
        topics=topics,
        recording_action_url="https://example.test/webhooks/twilio/recording?session_id=ivw_voice_X",
    )
    assert twiml == _EXPECTED_INITIAL_TWIML


def test_render_session_complete_twiml_has_hangup():
    body = voice_intake.render_session_complete_twiml()
    assert "<Hangup/>" in body
    assert "interview is complete" in body


def test_render_initial_twiml_rejects_empty_topics():
    with pytest.raises(voice_intake.VoiceIntakeError):
        voice_intake.render_initial_twiml(
            topics=(),
            recording_action_url="https://example.test/recording",
        )


# ---------------------------------------------------------------------------
# start_call
# ---------------------------------------------------------------------------


def test_start_call_persists_session_and_invokes_twilio(_reset_state):
    fake: _FakeTwilio = _reset_state
    _, app_id = _seed_application()

    db = SessionLocal()
    try:
        session = voice_intake.start_call(
            db,
            application_id=app_id,
            phone_number="+15551234567",
            from_number="+15550009999",
            voice_webhook_url="https://example.test/webhooks/twilio/voice",
            status_callback_url="https://example.test/webhooks/twilio/status",
        )
        db.commit()
    finally:
        db.close()

    assert session.id.startswith("ivw_voice_")
    assert session.application_id == app_id
    assert session.channel == "voice"
    assert session.status == "active"
    assert len(fake.placed) == 1
    assert fake.placed[0]["to"] == "+15551234567"
    assert fake.placed[0]["from_"] == "+15550009999"
    assert fake.placed[0]["twiml_url"].startswith("https://example.test/")


def test_start_call_rejects_missing_phone_number():
    _, app_id = _seed_application()
    db = SessionLocal()
    try:
        with pytest.raises(voice_intake.VoiceIntakeError):
            voice_intake.start_call(
                db,
                application_id=app_id,
                phone_number="",
                from_number="+15550009999",
                voice_webhook_url="x",
                status_callback_url="y",
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# store_recording + finalize_session
# ---------------------------------------------------------------------------


def _create_session(app_id: str) -> models.InterviewSession:
    db = SessionLocal()
    try:
        s = models.InterviewSession(
            id="ivw_voice_fixed",
            application_id=app_id,
            channel="voice",
            locale="en-US",
            status="active",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s
    finally:
        db.close()


def test_store_recording_writes_to_object_storage_and_db():
    _, app_id = _seed_application()
    session = _create_session(app_id)

    db = SessionLocal()
    try:
        rec = voice_intake.store_recording(
            db,
            session=session,
            topic_id="interview_opening",
            recording_sid="RE-fake-1",
            recording_bytes=b"\x00\x01\x02hello",
            duration_seconds=42.0,
        )
        db.commit()
        assert rec.recording_uri.startswith("coh://local/default/interviews/")
        assert len(rec.recording_sha256) == 64
        assert rec.duration_seconds == 42.0
        # Verify bytes round-trip through storage.
        assert object_storage.get(rec.recording_uri) == b"\x00\x01\x02hello"
    finally:
        db.close()


def test_finalize_session_emits_exactly_one_event_and_is_idempotent():
    _, app_id = _seed_application()
    session = _create_session(app_id)
    topics = voice_intake.load_topics()

    db = SessionLocal()
    try:
        for t in topics:
            voice_intake.store_recording(
                db,
                session=session,
                topic_id=t.id,
                recording_sid=f"RE-{t.id}",
                recording_bytes=f"audio-{t.id}".encode("utf-8"),
                duration_seconds=30.0,
            )
        # Refresh because store_recording may have set the session in another
        # session if necessary; here we just ensure the same row.
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        s = db.query(models.InterviewSession).filter_by(id=session.id).one()
        evt_id = voice_intake.finalize_session(
            db,
            session=s,
            topics=topics,
            provider_call_sid="CA-fake-sid",
        )
        db.commit()
        assert evt_id is not None

        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["application_id"] == app_id
        assert payload["session_id"] == session.id
        assert payload["topics_covered"] == len(topics)
        assert payload["topics_total"] == len(topics)
        assert payload["channel"] == "voice"
        assert payload["provider_call_sid"] == "CA-fake-sid"
        topic_ids = [t["topic_id"] for t in payload["topics"]]
        assert topic_ids == [t.id for t in topics]
    finally:
        db.close()

    # Second call should be a no-op.
    db = SessionLocal()
    try:
        s = db.query(models.InterviewSession).filter_by(id=session.id).one()
        evt_id_2 = voice_intake.finalize_session(
            db, session=s, topics=topics
        )
        db.commit()
        assert evt_id_2 is None
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .all()
        )
        assert len(events) == 1
    finally:
        db.close()
