"""Deployment-surface tests for workflow and serverless worker routes."""

from __future__ import annotations

import pytest

try:
    from fastapi.testclient import TestClient
except BaseException as _exc:  # pragma: no cover - arch mismatch / missing dep
    pytest.skip(f"FastAPI unavailable in this interpreter: {_exc}", allow_module_level=True)

from coherence_engine.server.fund.app import create_app
from coherence_engine.server.fund.routers import worker as worker_router


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("COHERENCE_FUND_AUTH_MODE", "disabled")
    return TestClient(create_app())


def test_workflow_router_is_mounted(client: TestClient) -> None:
    response = client.post("/api/v1/workflow/missing/run")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_legacy_workflow_router_is_mounted(client: TestClient) -> None:
    response = client.post("/workflow/missing/run")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_worker_cron_requires_configured_secret(client: TestClient) -> None:
    response = client.get("/api/v1/worker/scoring/process-once")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WORKER_CRON_NOT_CONFIGURED"


def test_worker_cron_rejects_invalid_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    response = client.get(
        "/api/v1/worker/scoring/process-once",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_worker_cron_processes_once_with_bearer_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr(
        worker_router,
        "process_once",
        lambda **_: {"processed": 2, "failed": 0, "idle": 1},
    )

    response = client.get(
        "/api/v1/worker/scoring/process-once?max_jobs=2",
        headers={"Authorization": "Bearer correct-secret"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"processed": 2, "failed": 0, "idle": 1}
