"""Tests for the dropped-call recovery flow (prompt 41)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import interview_recovery as recovery


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _seed_session(db, *, status: str = "active", state: dict | None = None,
                  created_at: datetime | None = None) -> models.InterviewSession:
    founder = models.Founder(
        id="f_rec",
        full_name="R F",
        email="r@example.com",
        company_name="R Co",
        country="US",
    )
    app = models.Application(
        id="app_rec",
        founder_id=founder.id,
        one_liner="x",
        requested_check_usd=1,
        use_of_funds_summary="x",
        preferred_channel="voice",
    )
    db.add(founder)
    db.add(app)
    sess = models.InterviewSession(
        id="ivw_voice_rec",
        application_id=app.id,
        channel="voice",
        locale="en-US",
        status=status,
        state_json=json.dumps(state or {}, sort_keys=True),
    )
    if created_at is not None:
        sess.created_at = created_at
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def _state_with_next_question(topic_id: str = "evidence", **overrides) -> dict:
    state = {
        "policy_version": "1",
        "graph_version": "1",
        "started_at": "2026-04-25T00:00:00+00:00",
        "duration_seconds_used": 30.0,
        "duration_seconds_cap": 1800,
        "next_question": {
            "topic_id": topic_id,
            "prompt": "resume here",
            "kind": "primary",
        },
        "topics": {
            "problem": {"asked": True, "answered": True, "confidence": 0.8,
                         "evidence_score": 0.8, "anti_gaming_flagged": False,
                         "follow_ups_asked": 0},
        },
        "anti_gaming_count": 0,
        "recovery_attempts": 0,
        "completed": False,
    }
    state.update(overrides)
    return state


def test_should_recover_returns_true_for_fresh_incomplete_session():
    db = SessionLocal()
    try:
        sess = _seed_session(db, state=_state_with_next_question())
        assert recovery.should_recover(sess) is True
    finally:
        db.close()


def test_should_recover_false_when_completed():
    db = SessionLocal()
    try:
        sess = _seed_session(
            db, status="completed",
            state=_state_with_next_question(completed=True),
        )
        assert recovery.should_recover(sess) is False
    finally:
        db.close()


def test_should_recover_false_outside_window():
    db = SessionLocal()
    try:
        old = datetime.now(tz=timezone.utc) - timedelta(hours=48)
        sess = _seed_session(db, state=_state_with_next_question(),
                              created_at=old)
        assert recovery.should_recover(sess) is False
    finally:
        db.close()


def test_recover_session_resumes_from_next_question():
    db = SessionLocal()
    try:
        sess = _seed_session(db, state=_state_with_next_question("evidence"))

        notified: list = []
        dialed: list = []

        def notifier(s, q):
            notified.append((s.id, q.topic_id))

        def redialer(s, q):
            dialed.append((s.id, q.topic_id))
            return "CA-resume"

        result = recovery.recover_session(
            db, sess, notifier=notifier, redialer=redialer
        )
        assert result.resumed_topic_id == "evidence"
        assert result.recovery_attempts == 1
        assert result.notification_sent is True
        assert notified == [(sess.id, "evidence")]
        assert dialed == [(sess.id, "evidence")]
        # State persisted with bumped attempts counter.
        db.refresh(sess)
        state = json.loads(sess.state_json)
        assert state["recovery_attempts"] == 1
    finally:
        db.close()


def test_recover_refuses_second_attempt():
    db = SessionLocal()
    try:
        st = _state_with_next_question("evidence", recovery_attempts=1)
        sess = _seed_session(db, state=st)
        with pytest.raises(recovery.RecoveryRefused):
            recovery.recover_session(db, sess)
    finally:
        db.close()


def test_recover_refuses_when_no_next_question():
    db = SessionLocal()
    try:
        st = _state_with_next_question("evidence")
        st["next_question"] = None
        # Coverage still incomplete so should_recover may still pass —
        # the explicit RecoveryRefused for the missing question is the
        # contract we are checking.
        st["topics"]["problem"]["confidence"] = 0.95
        sess = _seed_session(db, state=st)
        with pytest.raises(recovery.RecoveryRefused):
            recovery.recover_session(db, sess)
    finally:
        db.close()


def test_resume_question_round_trip():
    db = SessionLocal()
    try:
        sess = _seed_session(db, state=_state_with_next_question("self_critique"))
        q = recovery.resume_question(sess)
        assert q is not None
        assert q.topic_id == "self_critique"
    finally:
        db.close()


def test_recover_works_without_notifier_or_redialer():
    db = SessionLocal()
    try:
        sess = _seed_session(db, state=_state_with_next_question())
        result = recovery.recover_session(db, sess)
        assert result.notification_sent is False
        assert result.recovery_attempts == 1
    finally:
        db.close()
