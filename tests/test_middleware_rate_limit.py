"""Tests for the per-IP / per-API-key rate-limit middleware (prompt 37)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.middleware.rate_limit import (
    RATE_LIMITER,
    RateLimitMiddleware,
)


@pytest.fixture(autouse=True)
def _reset_limiter():
    old_default = settings.rate_limit_default
    RATE_LIMITER.reset()
    yield
    settings.rate_limit_default = old_default
    RATE_LIMITER.reset()


def _build_app(limit: int) -> TestClient:
    settings.rate_limit_default = limit
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/echo")
    def echo():
        return {"ok": True}

    return TestClient(app)


def test_per_ip_bucket_returns_429_after_limit_exhausted():
    client = _build_app(limit=3)
    statuses = [client.get("/echo").status_code for _ in range(5)]
    assert statuses.count(200) == 3
    assert statuses.count(429) == 2


def test_429_response_carries_retry_after_header_and_json_body():
    client = _build_app(limit=2)
    for _ in range(2):
        assert client.get("/echo").status_code == 200
    response = client.get("/echo")
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) >= 1
    body = response.json()
    assert body["error"] == "rate_limited"
    assert isinstance(body["retry_after_seconds"], int)
    assert body["retry_after_seconds"] >= 1


def test_distinct_api_key_prefixes_get_independent_buckets():
    client = _build_app(limit=2)
    # First prefix exhausts its quota.
    for _ in range(2):
        assert client.get("/echo", headers={"X-API-Key": "AAAAAAAA-rest"}).status_code == 200
    assert client.get("/echo", headers={"X-API-Key": "AAAAAAAA-rest"}).status_code == 429
    # A different prefix still has a fresh bucket.
    assert client.get("/echo", headers={"X-API-Key": "BBBBBBBB-rest"}).status_code == 200


def test_health_endpoints_skip_rate_limit():
    settings.rate_limit_default = 1
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/health")
    def health():
        return {"ok": True}

    client = TestClient(app)
    for _ in range(5):
        assert client.get("/health").status_code == 200
