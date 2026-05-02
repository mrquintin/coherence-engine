"""Tests for outbox/scoring worker ops telemetry snapshots."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture()
def fund_session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()


def test_outbox_get_ops_metrics_empty(fund_session: Session):
    from coherence_engine.server.fund.repositories.outbox_repository import OutboxRepository

    repo = OutboxRepository(fund_session)
    m = repo.get_ops_metrics()
    assert m["pending_dispatchable"] == 0
    assert m["oldest_pending_age_seconds"] is None
    assert m["failed_dlq"] == 0


def test_outbox_get_ops_metrics_pending_and_failed(fund_session: Session):
    from coherence_engine.server.fund.repositories.outbox_repository import OutboxRepository

    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(minutes=30)
    fund_session.add(
        models.EventOutbox(
            id=_new_id(),
            event_type="ApplicationCreated",
            event_version="1",
            producer="test",
            trace_id="t1",
            idempotency_key="ik1",
            payload_json="{}",
            status="pending",
            attempts=0,
            occurred_at=old,
        )
    )
    fund_session.add(
        models.EventOutbox(
            id=_new_id(),
            event_type="ApplicationCreated",
            event_version="1",
            producer="test",
            trace_id="t2",
            idempotency_key="ik2",
            payload_json="{}",
            status="failed",
            attempts=5,
            occurred_at=now,
        )
    )
    fund_session.commit()

    repo = OutboxRepository(fund_session)
    m = repo.get_ops_metrics()
    assert m["pending_dispatchable"] == 1
    assert m["oldest_pending_age_seconds"] is not None
    assert m["oldest_pending_age_seconds"] >= 29 * 60
    assert m["failed_dlq"] == 1


def test_emit_outbox_ops_snapshot_warn_tags(fund_session: Session, monkeypatch):
    from coherence_engine.server.fund.repositories.outbox_repository import OutboxRepository
    from coherence_engine.server.fund.services.outbox_dispatcher import emit_outbox_ops_snapshot

    now = datetime.now(tz=timezone.utc)
    fund_session.add(
        models.EventOutbox(
            id=_new_id(),
            event_type="ApplicationCreated",
            event_version="1",
            producer="test",
            trace_id="t1",
            idempotency_key="ik1",
            payload_json="{}",
            status="pending",
            attempts=0,
            occurred_at=now,
        )
    )
    fund_session.commit()

    monkeypatch.setenv("COHERENCE_FUND_OUTBOX_OPS_QUEUE_WARN_DEPTH", "1")
    repo = OutboxRepository(fund_session)
    data = emit_outbox_ops_snapshot(
        repo, tick_result={"published": 0, "failed": 0, "scanned": 0}
    )
    assert data["marker"] == "COHERENCE_FUND_WORKER_OPS_SNAPSHOT"
    assert data["component"] == "outbox"
    assert "queue_depth" in data["warn"]


def test_scoring_ops_metrics_and_snapshot(fund_session: Session, monkeypatch):
    from coherence_engine.server.fund.scoring_worker import collect_scoring_ops_metrics, emit_scoring_ops_snapshot

    founder = models.Founder(
        id=_new_id(),
        full_name="Test Founder",
        email="t@example.com",
        company_name="Co",
        country="US",
    )
    app = models.Application(
        id=_new_id(),
        founder_id=founder.id,
        one_liner="x",
        requested_check_usd=1,
        use_of_funds_summary="y",
        preferred_channel="email",
    )
    fund_session.add_all([founder, app])
    fund_session.add(
        models.ScoringJob(
            id=_new_id(),
            application_id=app.id,
            mode="full",
            dry_run=False,
            status="queued",
            attempts=0,
            max_attempts=5,
        )
    )
    fund_session.commit()

    m = collect_scoring_ops_metrics(fund_session)
    assert m["eligible_queue_depth"] == 1
    assert m["failed_dlq"] == 0

    monkeypatch.setenv("COHERENCE_FUND_SCORING_OPS_QUEUE_WARN_DEPTH", "1")
    data = emit_scoring_ops_snapshot(
        fund_session, tick_result={"processed": 0, "failed": 0, "idle": 1}
    )
    assert data["marker"] == "COHERENCE_FUND_WORKER_OPS_SNAPSHOT"
    assert data["component"] == "scoring"
    assert "queue_depth" in data["warn"]


def test_scoring_worker_db_healthcheck(fund_session: Session, monkeypatch):
    from coherence_engine.server.fund import scoring_worker

    monkeypatch.setattr(scoring_worker, "SessionLocal", lambda: fund_session)
    assert scoring_worker.db_healthcheck() == 0
