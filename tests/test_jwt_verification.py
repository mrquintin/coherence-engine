"""Unit tests for Supabase JWT verification (prompt 25).

Covers:
* Happy path — valid RS256-signed token with the expected ``aud`` / ``iss``.
* Tampered signature — payload was altered after signing.
* Expired ``exp`` — token whose lifetime has already passed.
* Wrong ``aud`` — caller authenticated against a different project.
* Wrong ``iss`` — caller authenticated against a different issuer.
* JWKS unavailable — network-failure fallback maps to 503, not 500.

Tests construct an RSA-2048 keypair in-process, mint tokens with PyJWT,
and inject the public JWK into the cache directly so no HTTP fixture is
required.
"""

from __future__ import annotations

import json
import time

import pytest

try:
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except BaseException as _exc:  # pragma: no cover - arch mismatch / missing dep
    pytest.skip(
        f"PyJWT / cryptography unavailable in this interpreter: {_exc}",
        allow_module_level=True,
    )

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from coherence_engine.server.fund.security.auth import (
    AuthError,
    verify_supabase_jwt,
)
from coherence_engine.server.fund.security.jwks_cache import (
    JWKSUnavailable,
    JwksCache,
)


KID = "test-key-1"
AUD = "authenticated"
ISS = "https://test.supabase.co/auth/v1"


def _gen_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _public_jwk(public_key, kid: str = KID) -> dict:
    jwk_str = pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key)
    jwk = json.loads(jwk_str) if isinstance(jwk_str, str) else dict(jwk_str)
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return jwk


def _mint_token(private_key, *, claims_overrides=None, kid: str = KID, alg: str = "RS256") -> str:
    now = int(time.time())
    claims = {
        "sub": "user_abc",
        "email": "founder@example.com",
        "aud": AUD,
        "iss": ISS,
        "iat": now,
        "nbf": now - 5,
        "exp": now + 600,
    }
    if claims_overrides:
        claims.update(claims_overrides)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pyjwt.encode(claims, pem, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def keypair():
    return _gen_keypair()


@pytest.fixture
def cache(keypair):
    _, public_key = keypair
    c = JwksCache(jwks_url="http://example.invalid/jwks.json")
    c.set_key_for_test(KID, _public_jwk(public_key))
    return c


def test_verify_happy_path(keypair, cache):
    private_key, _ = keypair
    token = _mint_token(private_key)
    claims = verify_supabase_jwt(token, cache=cache, audience=AUD, issuer=ISS)
    assert claims["sub"] == "user_abc"
    assert claims["email"] == "founder@example.com"


def test_tampered_signature_rejected(keypair, cache):
    private_key, _ = keypair
    token = _mint_token(private_key)
    head, payload, sig = token.split(".")
    # Flip a bit in the signature.
    bad_sig = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB")
    tampered = ".".join([head, payload, bad_sig])
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(tampered, cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 401


def test_expired_exp_rejected(keypair, cache):
    private_key, _ = keypair
    past = int(time.time()) - 3600
    token = _mint_token(
        private_key, claims_overrides={"exp": past, "iat": past - 60, "nbf": past - 60}
    )
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(token, cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 401


def test_wrong_aud_returns_403(keypair, cache):
    private_key, _ = keypair
    token = _mint_token(private_key, claims_overrides={"aud": "some-other-aud"})
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(token, cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 403


def test_wrong_iss_returns_403(keypair, cache):
    private_key, _ = keypair
    token = _mint_token(private_key, claims_overrides={"iss": "https://evil.example/"})
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(token, cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 403


def test_jwks_unavailable_returns_503(keypair):
    """Empty cache + unreachable JWKS endpoint maps to 503."""
    private_key, _ = keypair
    token = _mint_token(private_key)
    empty_cache = JwksCache(jwks_url="http://127.0.0.1:1/does-not-exist.json")
    # Override the fetch to deterministically simulate network failure.
    empty_cache._do_fetch = lambda: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        JWKSUnavailable("network failure")
    )
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(token, cache=empty_cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 503


def test_hmac_token_rejected(keypair, cache):
    """HS256 tokens must be rejected — only asymmetric algs are allowed."""
    now = int(time.time())
    claims = {
        "sub": "user_abc",
        "aud": AUD,
        "iss": ISS,
        "iat": now,
        "exp": now + 600,
    }
    token = pyjwt.encode(
        claims, "shared-secret", algorithm="HS256", headers={"kid": KID}
    )
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(token, cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 401


def test_missing_token_returns_401(cache):
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt("", cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 401


def test_missing_kid_returns_401(keypair, cache):
    private_key, _ = keypair
    now = int(time.time())
    claims = {"sub": "u", "aud": AUD, "iss": ISS, "iat": now, "exp": now + 600}
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = pyjwt.encode(claims, pem, algorithm="RS256")  # no kid header
    with pytest.raises(AuthError) as ei:
        verify_supabase_jwt(token, cache=cache, audience=AUD, issuer=ISS)
    assert ei.value.status_code == 401
