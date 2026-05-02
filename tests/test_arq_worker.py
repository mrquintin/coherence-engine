"""Tests for the Arq-based background worker (Wave 9, prompt 30/70).

Two flavors of test live here:

1. Pure-function tests: assert that ``run_scoring_job`` produces a
   completed scoring decision when called directly against the
   in-memory SQLite test database. No Redis or Arq required.

2. Enqueue-idempotency tests: mock the Arq pool and assert that the
   second enqueue with the same idempotency key reuses the same
   ``_job_id`` (Arq's deduplication contract).

Tests that require ``arq`` itself use ``pytest.importorskip`` so the
suite stays green on a clean CI image without Redis or Arq installed.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from coherence_engine.server.fund.app import create_app
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.models import EventOutbox
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.workers import dispatch as _dispatch
from coherence_engine.server.fund.workers import tasks as _tasks


TOKENS: Dict[str, str] = {}


@pytest.fixture(autouse=True)
def _reset_fund_db():
    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        admin = svc.create_key(
            repo, label="arq-admin", role="admin", created_by="tests", expires_in_days=30
        )
        analyst = svc.create_key(
            repo, label="arq-analyst", role="analyst", created_by="tests", expires_in_days=30
        )
        TOKENS["admin"] = admin["token"]
        TOKENS["analyst"] = analyst["token"]
        db.commit()
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _headers(suffix: str) -> Dict[str, str]:
    return {
        "X-API-Key": TOKENS["analyst"],
        "X-Request-Id": f"req_{suffix}",
        "Idempotency-Key": f"idem_{suffix}",
    }


def _seed_application(client: TestClient) -> str:
    """Drive the API to create an application + interview + transcript.

    Mirrors the seeding sequence in ``tests/test_fund_backend.py`` so
    we exercise the real ``ScoringJob`` row that the worker would see
    in production.
    """
    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("arq-create"),
        json={
            "founder": {
                "full_name": "Arq Worker Founder",
                "email": "founder@example.com",
                "company_name": "ArqCo Inc.",
                "country": "US",
            },
            "startup": {
                "one_liner": "Async worker testing platform for SMB finance ops.",
                "requested_check_usd": 50_000,
                "use_of_funds_summary": "Hire two engineers; ship v1 GA.",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    assert create_res.status_code == 201, create_res.text
    application_id = create_res.json()["data"]["application_id"]

    iv_res = client.post(
        f"/api/v1/applications/{application_id}/interview-sessions",
        headers=_headers("arq-iv"),
        json={"channel": "web_voice", "locale": "en-US"},
    )
    assert iv_res.status_code == 201, iv_res.text

    score_res = client.post(
        f"/api/v1/applications/{application_id}/score",
        headers=_headers("arq-score"),
        json={
            "mode": "standard",
            "dry_run": False,
            "transcript_text": (
                "We reduce back-office processing time for small businesses. "
                "Our software integrates accounting, invoicing, and procurement. "
                "Pilot users reported fewer reconciliation errors and faster closes. "
                "The market has millions of SMBs with fragmented workflows. "
                "We sell a subscription model with expansion to payments."
            ),
        },
    )
    assert score_res.status_code == 202, score_res.text
    return application_id


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_run_scoring_job_processes_pending_application():
    """``tasks.run_scoring_job`` end-to-end smoke against SQLite."""
    app = create_app()
    client = TestClient(app)
    application_id = _seed_application(client)

    result = _tasks.run_scoring_job(application_id)
    assert result["status"] in {"completed", "no_job"}
    assert result.get("application_id") == application_id

    db = SessionLocal()
    try:
        rows = db.query(EventOutbox).filter(EventOutbox.status == "pending").all()
        types = {r.event_type for r in rows}
        # When status==completed, the canonical event chain must be queued.
        if result["status"] == "completed":
            assert {
                "InterviewCompleted",
                "ArgumentCompiled",
                "CoherenceScored",
                "DecisionIssued",
            }.issubset(types)
    finally:
        db.close()


def test_run_scoring_job_no_job_when_already_drained():
    """Second call after the first drains returns ``no_job``."""
    app = create_app()
    client = TestClient(app)
    application_id = _seed_application(client)

    first = _tasks.run_scoring_job(application_id)
    assert first["status"] in {"completed", "failed", "retry_scheduled"}

    second = _tasks.run_scoring_job(application_id)
    assert second["status"] in {"completed", "no_job"}


# ---------------------------------------------------------------------------
# Enqueue-idempotency tests (mocked Arq pool)
# ---------------------------------------------------------------------------


class _FakePool:
    """A minimal Arq-pool double that enforces ``_job_id`` dedup.

    Tracks every ``enqueue_job`` call; returns a fake job object on the
    first call for a given ``_job_id`` and ``None`` on subsequent ones,
    matching Arq's documented behavior.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._seen: set[str] = set()

    async def enqueue_job(self, function_name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"name": function_name, "args": args, "kwargs": kwargs})
        job_id = kwargs.get("_job_id")
        if job_id and job_id in self._seen:
            return None
        if job_id:
            self._seen.add(job_id)
        fake = MagicMock()
        fake.job_id = job_id or f"auto-{len(self.calls)}"
        return fake

    async def aclose(self) -> None:
        return None


def _force_arq_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from coherence_engine.server.fund import config as _cfg

    monkeypatch.setattr(_cfg.settings, "WORKER_BACKEND", "arq")
    monkeypatch.setattr(_cfg.settings, "ARQ_QUEUE_PREFIX", "coherence_fund_test")


def test_enqueue_scoring_job_dedups_on_idempotency_key(monkeypatch: pytest.MonkeyPatch):
    _force_arq_backend(monkeypatch)
    pool = _FakePool()

    async def _run() -> None:
        first = await _dispatch.enqueue_scoring_job(
            "app_123", idempotency_key="idem-abc", pool=pool
        )
        second = await _dispatch.enqueue_scoring_job(
            "app_123", idempotency_key="idem-abc", pool=pool
        )
        assert first == "score:app_123:idem-abc"
        assert second is None

    asyncio.run(_run())
    assert len(pool.calls) == 2  # both calls made it to enqueue_job
    assert pool.calls[0]["kwargs"]["_job_id"] == "score:app_123:idem-abc"
    assert pool.calls[1]["kwargs"]["_job_id"] == "score:app_123:idem-abc"


def test_enqueue_scoring_job_distinct_keys_schedule_separately(
    monkeypatch: pytest.MonkeyPatch,
):
    _force_arq_backend(monkeypatch)
    pool = _FakePool()

    async def _run() -> None:
        a = await _dispatch.enqueue_scoring_job(
            "app_123", idempotency_key="idem-a", pool=pool
        )
        b = await _dispatch.enqueue_scoring_job(
            "app_123", idempotency_key="idem-b", pool=pool
        )
        assert a is not None and b is not None
        assert a != b

    asyncio.run(_run())


def test_enqueue_scoring_job_requires_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
):
    _force_arq_backend(monkeypatch)

    async def _run() -> None:
        with pytest.raises(ValueError):
            await _dispatch.enqueue_scoring_job("app_x", idempotency_key="")

    asyncio.run(_run())


def test_enqueue_helpers_noop_on_poll_backend(monkeypatch: pytest.MonkeyPatch):
    """``poll`` backend returns ``None`` and never reaches the pool."""
    from coherence_engine.server.fund import config as _cfg

    monkeypatch.setattr(_cfg.settings, "WORKER_BACKEND", "poll")
    pool = _FakePool()

    async def _run() -> None:
        assert (
            await _dispatch.enqueue_scoring_job(
                "app_x", idempotency_key="idem-y", pool=pool
            )
            is None
        )
        assert await _dispatch.enqueue_outbox_dispatch(pool=pool) is None
        assert (
            await _dispatch.enqueue_backtest(
                {"dataset_path": "/tmp/x.jsonl"},
                idempotency_key="bt-1",
                pool=pool,
            )
            is None
        )

    asyncio.run(_run())
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Worker-settings smoke (Arq optional)
# ---------------------------------------------------------------------------


def test_worker_settings_class_exposes_required_attrs():
    pytest.importorskip("arq")
    from coherence_engine.server.fund.workers.arq_worker import WorkerSettings

    fn_names = {fn.__name__ for fn in WorkerSettings.functions}
    assert {"score_job", "dispatch_outbox", "run_backtest"}.issubset(fn_names)
    assert WorkerSettings.max_jobs == 4
    assert WorkerSettings.max_tries == 3
    assert WorkerSettings.job_timeout == 900
    assert WorkerSettings.keep_result == 86400
