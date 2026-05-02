"""Integration tests for founder JWT route gating (prompt 25).

Drives the FastAPI ``TestClient`` end-to-end:

* Missing ``Authorization`` header → ``401``.
* Wrong ``aud`` → ``403``.
* Valid token, founder A creates an application → ``201``, scoped to A.
* Founder B tries to read founder A's application → ``403``.
* ``GET /healthz`` and ``GET /readyz`` work with no auth header.

The route gating tests build a minimal FastAPI app that mounts only the
applications + health routers — no ``FundSecurityMiddleware``. That keeps
the test focused on the :func:`current_founder` dependency contract; the
middleware still gates service-role traffic in the full ``create_app``
production wiring (covered by ``test_fund_backend.py``). The JWKS cache
is pre-populated with a hand-rolled RSA public key so we never make a
network call.
"""

from __future__ import annotations

import json
import time

import pytest

try:
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except BaseException as _exc:  # pragma: no cover - arch mismatch / missing dep
    pytest.skip(
        f"FastAPI / PyJWT / cryptography unavailable in this interpreter: {_exc}",
        allow_module_level=True,
    )

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund.database import Base, engine
from coherence_engine.server.fund.routers.applications import (
    router as applications_router,
)
from coherence_engine.server.fund.routers.health import router as health_router
from coherence_engine.server.fund.security.jwks_cache import (
    get_default_cache,
    reset_default_cache_for_tests,
)


KID = "test-key-route-1"
AUD = "authenticated"
ISS = "https://test-routes.supabase.co/auth/v1"


def _public_jwk(public_key, kid: str = KID) -> dict:
    jwk_str = pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key)
    jwk = json.loads(jwk_str) if isinstance(jwk_str, str) else dict(jwk_str)
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return jwk


def _mint_token(private_key, *, sub: str, email: str = "u@example.com",
                aud: str = AUD, iss: str = ISS, exp_offset: int = 600,
                kid: str = KID) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "email": email,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "nbf": now - 5,
        "exp": now + exp_offset,
    }
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pyjwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def _env_setup(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_AUD", AUD)
    monkeypatch.setenv("SUPABASE_JWT_ISS", ISS)
    monkeypatch.setenv("SUPABASE_JWKS_URL", "http://test.invalid/jwks.json")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_default_cache_for_tests()
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    reset_default_cache_for_tests()


@pytest.fixture
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_loaded(keypair):
    cache = get_default_cache()
    cache.set_key_for_test(KID, _public_jwk(keypair.public_key()))
    return cache


def _make_app() -> FastAPI:
    """Minimal app: routers only, no FundSecurityMiddleware.

    The middleware's API-key gate is exercised by ``test_fund_backend.py``;
    here we want to isolate the JWT dependency contract.
    """
    app = FastAPI()
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(applications_router, prefix="/api/v1")
    app.include_router(health_router)
    app.include_router(applications_router)
    return app


@pytest.fixture
def client():
    return TestClient(_make_app())


def _payload():
    return {
        "founder": {
            "full_name": "Jane Founder",
            "email": "jane@example.com",
            "company_name": "Acme Labs",
            "country": "US",
        },
        "startup": {
            "one_liner": "Workflow automation for SMB finance ops",
            "requested_check_usd": 50000,
            "use_of_funds_summary": "Hire engineers and run pilots",
            "preferred_channel": "web_voice",
        },
        "consent": {
            "ai_assessment": True,
            "recording": True,
            "data_processing": True,
        },
    }


def _headers(token: str | None = None, idem: str = "k1") -> dict:
    h = {"Idempotency-Key": idem, "X-Request-Id": f"req_{idem}"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def test_post_application_no_auth_returns_401(client, jwks_loaded):
    res = client.post("/api/v1/applications", headers=_headers(idem="k1"), json=_payload())
    assert res.status_code == 401


def test_post_application_wrong_aud_returns_403(client, keypair, jwks_loaded):
    bad = _mint_token(keypair, sub="u1", aud="wrong-aud")
    res = client.post(
        "/api/v1/applications", headers=_headers(token=bad, idem="k2"), json=_payload()
    )
    assert res.status_code == 403


def test_post_and_read_scoped_to_founder(client, keypair, jwks_loaded):
    token_a = _mint_token(keypair, sub="founder-a", email="a@example.com")
    res = client.post(
        "/api/v1/applications",
        headers=_headers(token=token_a, idem="ka"),
        json=_payload(),
    )
    assert res.status_code == 201, res.text
    body = res.json()["data"]
    application_id = body["application_id"]
    founder_a_id = body["founder_id"]

    # Same founder reads OK.
    read = client.get(
        f"/api/v1/applications/{application_id}",
        headers=_headers(token=token_a, idem="r1"),
    )
    assert read.status_code == 200
    assert read.json()["data"]["founder_id"] == founder_a_id


def test_other_founder_cannot_read(client, keypair, jwks_loaded):
    token_a = _mint_token(keypair, sub="founder-a", email="a@example.com")
    create = client.post(
        "/api/v1/applications",
        headers=_headers(token=token_a, idem="kc"),
        json=_payload(),
    )
    assert create.status_code == 201
    application_id = create.json()["data"]["application_id"]

    token_b = _mint_token(keypair, sub="founder-b", email="b@example.com")
    res = client.get(
        f"/api/v1/applications/{application_id}",
        headers=_headers(token=token_b, idem="rb"),
    )
    assert res.status_code == 403


def test_healthz_unauthenticated_ok(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "alive"


def test_readyz_unauthenticated_does_not_401(client, jwks_loaded):
    # JWKS cache has a key, DB is reachable → readiness should be 200.
    res = client.get("/readyz")
    assert res.status_code in (200, 503)
    # The key assertion: it must NOT be 401 (auth must not gate the probe).
    assert res.status_code != 401
