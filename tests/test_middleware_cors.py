"""Tests for the explicit CORS allow-list middleware (prompt 37)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.middleware.cors import install_cors
from coherence_engine.server.fund.middleware.request_id import RequestIdMiddleware


@pytest.fixture(autouse=True)
def _save_settings():
    saved_origins = settings.cors_allowed_origins
    saved_env = settings.environment
    yield
    settings.cors_allowed_origins = saved_origins
    settings.environment = saved_env


def _make_app(*, allow_origins: str, env: str = "test") -> TestClient:
    settings.cors_allowed_origins = allow_origins
    settings.environment = env  # type: ignore[assignment]
    app = FastAPI()
    install_cors(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    def echo():
        return {"ok": True}

    return TestClient(app)


def test_request_id_round_trip_uses_caller_supplied_id():
    client = _make_app(allow_origins="https://app.example.com")
    response = client.get("/echo", headers={"X-Request-ID": "req-fixed-123"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-fixed-123"


def test_request_id_minted_when_absent():
    client = _make_app(allow_origins="https://app.example.com")
    response = client.get("/echo")
    assert response.status_code == 200
    minted = response.headers.get("X-Request-ID", "")
    assert minted
    assert len(minted) >= 16


def test_preflight_from_allowed_origin_returns_allow_origin():
    client = _make_app(allow_origins="https://app.example.com")
    response = client.options(
        "/echo",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_preflight_from_non_allowed_origin_does_not_carry_allow_origin():
    client = _make_app(allow_origins="https://app.example.com")
    response = client.options(
        "/echo",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in {h.lower() for h in response.headers}


def test_wildcard_origin_outside_dev_raises_at_install_time():
    settings.cors_allowed_origins = "*"
    settings.environment = "prod"  # type: ignore[assignment]
    app = FastAPI()
    with pytest.raises(RuntimeError):
        install_cors(app)


def test_wildcard_origin_allowed_in_dev():
    client = _make_app(allow_origins="*", env="dev")
    response = client.options(
        "/echo",
        headers={
            "Origin": "https://anything.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") in {"*", "https://anything.example.com"}
