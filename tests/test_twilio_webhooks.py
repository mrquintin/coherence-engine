"""Twilio webhook router tests (prompt 38).

Mocked end-to-end: never calls a real Twilio API. Covers signature
verification (valid + invalid), the recording-callback object-storage
write, and end-of-session ``interview_session_completed`` emission
(exactly once).
"""

from __future__ import annotations

import os
from typing import Tuple

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.routers import twilio_webhooks
from coherence_engine.server.fund.routers.twilio_webhooks import (
    router as twilio_router,
    set_recording_fetcher_for_tests,
    reset_recording_fetcher_for_tests,
)
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services import voice_intake
from coherence_engine.server.fund.services.storage_backends import (
    LocalFilesystemBackend,
)
from coherence_engine.server.fund.services.twilio_adapter import (
    RequestValidator,
    verify_twilio_signature,
)


_AUTH_TOKEN = "test-twilio-auth-token"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(twilio_router)
    return app


@pytest.fixture(autouse=True)
def _reset_state(tmp_path):
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    os.environ["COHERENCE_FUND_ENV"] = "dev"
    os.environ["TWILIO_AUTH_TOKEN"] = _AUTH_TOKEN
    os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "true"

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="default")
    object_storage.set_object_storage(backend)

    set_recording_fetcher_for_tests(lambda sid, url: f"audio:{sid}".encode("utf-8"))

    yield

    reset_recording_fetcher_for_tests()
    object_storage.reset_object_storage()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    os.environ.pop("TWILIO_AUTH_TOKEN", None)
    os.environ.pop("TWILIO_VALIDATE_WEBHOOK_SIGNATURE", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(session_id: str = "ivw_voice_test1") -> Tuple[str, str]:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="f_w",
            full_name="W",
            email="w@x.com",
            company_name="W",
            country="US",
        )
        app = models.Application(
            id="app_w",
            founder_id="f_w",
            one_liner="x",
            requested_check_usd=1,
            use_of_funds_summary="x",
            preferred_channel="voice",
        )
        s = models.InterviewSession(
            id=session_id,
            application_id="app_w",
            channel="voice",
            locale="en-US",
            status="active",
        )
        db.add_all([founder, app, s])
        db.commit()
        return app.id, s.id
    finally:
        db.close()


def _sign(url: str, params: dict) -> str:
    validator = RequestValidator(_AUTH_TOKEN)
    return validator.compute_signature(url, params)


# ---------------------------------------------------------------------------
# Signature unit tests
# ---------------------------------------------------------------------------


def test_verify_twilio_signature_accepts_valid():
    url = "https://example.test/webhooks/twilio/voice?session_id=ivw_voice_test1"
    params = {"CallSid": "CA1", "From": "+15551112222"}
    sig = _sign(url, params)
    assert verify_twilio_signature(
        auth_token=_AUTH_TOKEN, url=url, params=params, signature_header=sig
    )


def test_verify_twilio_signature_rejects_tampered_param():
    url = "https://example.test/webhooks/twilio/voice"
    params = {"CallSid": "CA1"}
    sig = _sign(url, params)
    tampered = dict(params, CallSid="CA-evil")
    assert not verify_twilio_signature(
        auth_token=_AUTH_TOKEN, url=url, params=tampered, signature_header=sig
    )


def test_verify_twilio_signature_rejects_empty_token():
    url = "https://example.test/webhooks/twilio/voice"
    params = {"CallSid": "CA1"}
    sig = _sign(url, params)
    assert not verify_twilio_signature(
        auth_token="", url=url, params=params, signature_header=sig
    )


def test_verify_twilio_signature_rejects_missing_signature():
    url = "https://example.test/webhooks/twilio/voice"
    params = {"CallSid": "CA1"}
    assert not verify_twilio_signature(
        auth_token=_AUTH_TOKEN, url=url, params=params, signature_header=""
    )


# ---------------------------------------------------------------------------
# Voice TwiML route
# ---------------------------------------------------------------------------


def test_voice_route_renders_initial_twiml_with_valid_signature():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    public_url = f"http://testserver/webhooks/twilio/voice?session_id={sid}"
    params = {"CallSid": "CA-call-1", "From": "+15551112222"}
    sig = _sign(public_url, params)

    resp = client.post(
        f"/webhooks/twilio/voice?session_id={sid}",
        data=params,
        headers={"X-Twilio-Signature": sig},
    )
    assert resp.status_code == 200
    body = resp.text
    assert body.startswith('<?xml version="1.0"')
    assert "<Response>" in body
    assert "<Record" in body
    assert "session_id=" + sid in body
    assert "topic_id=interview_opening" in body


def test_voice_route_rejects_invalid_signature():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    resp = client.post(
        f"/webhooks/twilio/voice?session_id={sid}",
        data={"CallSid": "CA-1"},
        headers={"X-Twilio-Signature": "deadbeef"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_voice_route_404_when_session_unknown():
    client = TestClient(_build_app())
    public_url = "http://testserver/webhooks/twilio/voice?session_id=ivw_voice_unknown"
    params = {"CallSid": "CA-1"}
    sig = _sign(public_url, params)
    resp = client.post(
        "/webhooks/twilio/voice?session_id=ivw_voice_unknown",
        data=params,
        headers={"X-Twilio-Signature": sig},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Recording callback
# ---------------------------------------------------------------------------


def test_recording_callback_stores_blob_and_writes_row():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    url = (
        f"http://testserver/webhooks/twilio/recording"
        f"?session_id={sid}&topic_id=interview_opening"
    )
    params = {
        "CallSid": "CA-1",
        "RecordingSid": "RE-1",
        "RecordingUrl": "https://api.twilio.com/.../RE-1",
        "RecordingDuration": "55",
    }
    sig = _sign(url, params)
    resp = client.post(
        f"/webhooks/twilio/recording?session_id={sid}&topic_id=interview_opening",
        data=params,
        headers={"X-Twilio-Signature": sig},
    )
    assert resp.status_code == 200
    body = resp.text
    # Next topic prompt should be issued (self_critique).
    assert "topic_id=self_critique" in body

    # DB row written
    db = SessionLocal()
    try:
        rec = (
            db.query(models.InterviewRecording)
            .filter_by(session_id=sid, topic_id="interview_opening")
            .one()
        )
        assert rec.recording_uri.startswith("coh://local/")
        assert rec.duration_seconds == 55.0
        # Stored bytes match what the test fetcher returned.
        assert object_storage.get(rec.recording_uri) == b"audio:RE-1"
    finally:
        db.close()


def test_recording_callback_after_all_topics_returns_hangup():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    topics = voice_intake.load_topics()
    for t in topics:
        url = (
            f"http://testserver/webhooks/twilio/recording"
            f"?session_id={sid}&topic_id={t.id}"
        )
        params = {
            "CallSid": "CA-1",
            "RecordingSid": f"RE-{t.id}",
            "RecordingUrl": "https://api.twilio.com/RE",
            "RecordingDuration": "30",
        }
        sig = _sign(url, params)
        resp = client.post(
            f"/webhooks/twilio/recording?session_id={sid}&topic_id={t.id}",
            data=params,
            headers={"X-Twilio-Signature": sig},
        )
        assert resp.status_code == 200
    # Final POST after every topic recorded → final farewell + hangup
    assert "<Hangup/>" in resp.text


def test_recording_callback_rejects_invalid_signature_does_not_mutate():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    resp = client.post(
        f"/webhooks/twilio/recording?session_id={sid}&topic_id=interview_opening",
        data={"CallSid": "CA-1", "RecordingSid": "RE-1", "RecordingDuration": "1"},
        headers={"X-Twilio-Signature": "ZZZZ"},
    )
    assert resp.status_code == 401
    db = SessionLocal()
    try:
        n = db.query(models.InterviewRecording).count()
        assert n == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# End-of-session: status callback emits exactly one event
# ---------------------------------------------------------------------------


def test_status_callback_emits_session_completed_exactly_once():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    topics = voice_intake.load_topics()

    # Record every topic first so finalize_session reports topics_covered = total.
    for t in topics:
        url = (
            f"http://testserver/webhooks/twilio/recording"
            f"?session_id={sid}&topic_id={t.id}"
        )
        params = {
            "CallSid": "CA-1",
            "RecordingSid": f"RE-{t.id}",
            "RecordingUrl": "x",
            "RecordingDuration": "30",
        }
        sig = _sign(url, params)
        client.post(
            f"/webhooks/twilio/recording?session_id={sid}&topic_id={t.id}",
            data=params,
            headers={"X-Twilio-Signature": sig},
        )

    # Now POST a terminal call status — and a second one. Only one event.
    status_url = f"http://testserver/webhooks/twilio/status?session_id={sid}"
    status_params = {"CallSid": "CA-1", "CallStatus": "completed"}
    sig = _sign(status_url, status_params)
    for _ in range(2):
        resp = client.post(
            f"/webhooks/twilio/status?session_id={sid}",
            data=status_params,
            headers={"X-Twilio-Signature": sig},
        )
        assert resp.status_code == 200

    db = SessionLocal()
    try:
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .all()
        )
        assert len(events) == 1
        sess = db.query(models.InterviewSession).filter_by(id=sid).one()
        assert sess.status == "completed"
    finally:
        db.close()


def test_status_callback_non_terminal_does_not_emit_event():
    _, sid = _seed_session()
    client = TestClient(_build_app())
    status_url = f"http://testserver/webhooks/twilio/status?session_id={sid}"
    status_params = {"CallSid": "CA-1", "CallStatus": "ringing"}
    sig = _sign(status_url, status_params)
    resp = client.post(
        f"/webhooks/twilio/status?session_id={sid}",
        data=status_params,
        headers={"X-Twilio-Signature": sig},
    )
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "interview_session_completed")
            .count()
        )
        assert events == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Production env: signature verification cannot be disabled
# ---------------------------------------------------------------------------


def test_signature_check_required_in_staging_even_if_opted_out():
    from coherence_engine.server.fund import config as _cfg
    original = _cfg.settings.environment
    _cfg.settings.environment = "staging"
    os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "false"
    try:
        assert twilio_webhooks._signature_validation_required() is True
    finally:
        _cfg.settings.environment = original
        os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "true"


def test_signature_check_optional_only_in_dev():
    from coherence_engine.server.fund import config as _cfg
    original = _cfg.settings.environment
    _cfg.settings.environment = "dev"
    os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "false"
    try:
        assert twilio_webhooks._signature_validation_required() is False
    finally:
        _cfg.settings.environment = original
        os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "true"


def test_signature_check_required_in_prod_always():
    from coherence_engine.server.fund import config as _cfg
    original = _cfg.settings.environment
    _cfg.settings.environment = "prod"
    os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "false"
    try:
        assert twilio_webhooks._signature_validation_required() is True
    finally:
        _cfg.settings.environment = original
        os.environ["TWILIO_VALIDATE_WEBHOOK_SIGNATURE"] = "true"
