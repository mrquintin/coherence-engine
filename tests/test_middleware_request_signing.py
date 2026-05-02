"""Tests for HMAC-SHA-256 request signing on internal routes (prompt 37)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.middleware.request_signing import (
    REPLAY_CACHE,
    RequestSigningMiddleware,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
)


SECRET = "unit-test-signing-secret"


@pytest.fixture(autouse=True)
def _configure_secret():
    REPLAY_CACHE.reset()
    original = settings.request_signing_secret
    from pydantic import SecretStr

    settings.request_signing_secret = SecretStr(SECRET)
    yield
    settings.request_signing_secret = original
    REPLAY_CACHE.reset()


def _build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestSigningMiddleware)

    @app.post("/api/v1/internal/echo")
    async def echo(payload: dict):
        return {"ok": True, "echo": payload}

    @app.get("/api/v1/public/ping")
    def public_ping():
        return {"ok": True}

    return TestClient(app)


def _now_rfc3339(offset_seconds: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.isoformat().replace("+00:00", "Z")


def _signed_headers(method: str, path: str, body: bytes, *, ts: str | None = None) -> dict:
    ts = ts or _now_rfc3339()
    sig = compute_signature(SECRET, ts, method, path, body)
    return {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: f"v1={sig}",
        "Content-Type": "application/json",
    }


def test_valid_signature_passes():
    client = _build_client()
    body = b'{"hello":"world"}'
    headers = _signed_headers("POST", "/api/v1/internal/echo", body)
    response = client.post("/api/v1/internal/echo", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["echo"] == {"hello": "world"}


def test_tampered_body_returns_401():
    client = _build_client()
    body = b'{"hello":"world"}'
    headers = _signed_headers("POST", "/api/v1/internal/echo", body)
    tampered = b'{"hello":"evil"}'
    response = client.post("/api/v1/internal/echo", content=tampered, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_signature"


def test_replay_within_window_is_rejected():
    client = _build_client()
    body = b'{"hello":"world"}'
    headers = _signed_headers("POST", "/api/v1/internal/echo", body)
    first = client.post("/api/v1/internal/echo", content=body, headers=headers)
    assert first.status_code == 200
    second = client.post("/api/v1/internal/echo", content=body, headers=headers)
    assert second.status_code == 401
    assert "replay" in second.json()["message"].lower()


def test_skewed_timestamp_rejected():
    client = _build_client()
    body = b"{}"
    far_future = _now_rfc3339(offset_seconds=settings.REQUEST_SIGNING_MAX_SKEW_SECONDS + 60)
    headers = _signed_headers("POST", "/api/v1/internal/echo", body, ts=far_future)
    response = client.post("/api/v1/internal/echo", content=body, headers=headers)
    assert response.status_code == 401


def test_public_paths_do_not_require_signature():
    client = _build_client()
    response = client.get("/api/v1/public/ping")
    assert response.status_code == 200


def test_missing_headers_returns_401():
    client = _build_client()
    response = client.post(
        "/api/v1/internal/echo",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401
