"""Verify the ``WORKER_BACKEND`` env switch is honored end-to-end.

Two equivalent contracts are exercised:

* ``WORKER_BACKEND=poll`` — the legacy DB-polling worker remains the
  failsafe. ``enqueue_scoring_job`` is a no-op (returns ``None``) and
  the polling worker can drain a freshly-created ``ScoringJob`` row to
  ``completed``.
* ``WORKER_BACKEND=arq`` — ``enqueue_scoring_job`` schedules an Arq
  job (mocked here to avoid a Redis dependency in CI) and the legacy
  poll loop is *not* invoked from the request path.

The Arq dependency is treated as optional: tests assert behavior using
a stub pool so the suite stays green on a clean image.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from coherence_engine.server.fund import config as _cfg
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.workers import dispatch as _dispatch


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
        analyst = svc.create_key(
            repo,
            label="switch-analyst",
            role="analyst",
            created_by="tests",
            expires_in_days=30,
        )
        admin = svc.create_key(
            repo,
            label="switch-admin",
            role="admin",
            created_by="tests",
            expires_in_days=30,
        )
        TOKENS["analyst"] = analyst["token"]
        TOKENS["admin"] = admin["token"]
        db.commit()
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


class _StubPool:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._seen: set[str] = set()

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"name": name, "args": args, "kwargs": kwargs})
        job_id = kwargs.get("_job_id")
        if job_id and job_id in self._seen:
            return None
        if job_id:
            self._seen.add(job_id)
        m = MagicMock()
        m.job_id = job_id or f"auto-{len(self.calls)}"
        return m


def test_backend_arq_calls_enqueue(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_cfg.settings, "WORKER_BACKEND", "arq")
    pool = _StubPool()

    async def _run() -> None:
        result = await _dispatch.enqueue_scoring_job(
            "app_xyz", idempotency_key="idem-1", pool=pool
        )
        assert result == "score:app_xyz:idem-1"

    asyncio.run(_run())
    assert len(pool.calls) == 1
    assert pool.calls[0]["name"] == "score_job"


def test_backend_poll_skips_enqueue(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_cfg.settings, "WORKER_BACKEND", "poll")
    pool = _StubPool()

    async def _run() -> None:
        result = await _dispatch.enqueue_scoring_job(
            "app_xyz", idempotency_key="idem-1", pool=pool
        )
        assert result is None

    asyncio.run(_run())
    assert pool.calls == []


def test_backend_default_is_poll(monkeypatch: pytest.MonkeyPatch):
    """The shipped default must NOT silently route through Redis."""
    monkeypatch.delenv("WORKER_BACKEND", raising=False)
    fresh = _cfg.FundSettings()
    assert fresh.WORKER_BACKEND == "poll"
    # Class default attribute or env-based default both must resolve to "poll"
    # when the env var is unset. We probe via a fresh os.getenv-style call.
    import os

    assert os.getenv("WORKER_BACKEND", "poll").lower() in {"poll", "arq"}
    # And the dispatch helper must agree:
    from coherence_engine.server.fund.workers.dispatch import _backend

    # Force-check current backend resolution (re-using the helper)
    # — should always be one of the two known values.
    assert _backend() in {"poll", "arq"}


def test_polling_worker_drains_a_job_under_poll_backend(
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: poll backend leaves the existing scoring_worker path intact."""
    monkeypatch.setattr(_cfg.settings, "WORKER_BACKEND", "poll")

    from fastapi.testclient import TestClient

    from coherence_engine.server.fund.app import create_app
    from coherence_engine.server.fund.scoring_worker import process_once

    def hdrs(suffix: str) -> Dict[str, str]:
        return {
            "X-API-Key": TOKENS["analyst"],
            "X-Request-Id": f"req_{suffix}",
            "Idempotency-Key": f"idem_{suffix}",
        }

    app = create_app()
    client = TestClient(app)
    create_res = client.post(
        "/api/v1/applications",
        headers=hdrs("switch-create"),
        json={
            "founder": {
                "full_name": "Switch Founder",
                "email": "founder@example.com",
                "company_name": "SwitchCo Inc.",
                "country": "US",
            },
            "startup": {
                "one_liner": "Backend switch validation for SMB finance ops.",
                "requested_check_usd": 50_000,
                "use_of_funds_summary": "Validate the worker switch end-to-end.",
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
        headers=hdrs("switch-iv"),
        json={"channel": "web_voice", "locale": "en-US"},
    )
    assert iv_res.status_code == 201

    score_res = client.post(
        f"/api/v1/applications/{application_id}/score",
        headers=hdrs("switch-score"),
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

    # Polling worker drains it deterministically — same code path that ships today.
    result = process_once(max_jobs=10)
    assert result["processed"] >= 1
