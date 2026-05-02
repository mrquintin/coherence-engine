"""Supabase JWT verification and the ``current_founder`` FastAPI dependency.

Supabase issues asymmetric (RS256 / ES256) JWTs for end-user
``authenticated`` calls — we never accept HMAC-signed tokens here, since the
HMAC secret is shared with every service-role caller and a leak would let an
attacker forge tokens for any user.

Layered with the existing API-key middleware
--------------------------------------------

The ``FundSecurityMiddleware`` continues to gate service-role traffic
(workers, admin) by API key. The :func:`current_founder` dependency on
*founder-portal* routes adds a second layer that verifies the Supabase JWT
in the ``Authorization: Bearer ...`` header, maps the ``sub`` claim onto
``Founder.founder_user_id``, and lazily upserts a Founder row on first call.
The router still does a defense-in-depth ownership check
(``application.founder_id == founder.id``) so we never depend on RLS alone.

Failure mapping
---------------

* 401 ``UNAUTHORIZED`` — missing / malformed / expired / tampered token.
* 403 ``FORBIDDEN`` — token is structurally valid but has the wrong
  ``aud`` or ``iss``: the caller authenticated against a different
  Supabase project / audience than this service trusts.
* 503 ``JWKS_UNAVAILABLE`` — the JWKS endpoint is unreachable and we have
  no cached keys, so we cannot verify *anyone*'s token. This is a
  service-availability problem, not an auth failure.

Logging discipline
------------------

We never log the raw token or the ``Authorization`` header. Audit logs
truncate to ``sub=<sub>`` so token material does not leak through log
aggregation.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.models import Founder
from coherence_engine.server.fund.security.jwks_cache import (
    JWKSUnavailable,
    JwksCache,
    get_default_cache,
)

LOGGER = logging.getLogger("coherence_engine.fund.auth")

JWT_CLOCK_SKEW_SECONDS = 30
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")


def _supabase_aud() -> str:
    return os.getenv("SUPABASE_JWT_AUD", "authenticated").strip()


def _supabase_iss() -> str:
    return os.getenv("SUPABASE_JWT_ISS", "").strip()


def _truncate_sub(sub: Optional[str]) -> str:
    if not sub:
        return "<missing>"
    s = str(sub)
    return s if len(s) <= 36 else s[:36]


class AuthError(HTTPException):
    """Typed wrapper so the router layer can format a consistent envelope."""


def _unauthorized(reason: str) -> AuthError:
    return AuthError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": reason},
    )


def _forbidden(reason: str) -> AuthError:
    return AuthError(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "FORBIDDEN", "message": reason},
    )


def _jwks_unavailable(reason: str) -> AuthError:
    return AuthError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "JWKS_UNAVAILABLE", "message": reason},
    )


def _import_jwt():
    try:
        import jwt as pyjwt  # PyJWT
        from jwt import algorithms as jwt_algorithms
        from jwt import exceptions as jwt_exceptions
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise AuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AUTH_DEPENDENCY_MISSING",
                "message": f"PyJWT is required for Supabase auth: {exc}",
            },
        ) from exc
    return pyjwt, jwt_algorithms, jwt_exceptions


def _signing_key_from_jwk(jwk: Dict[str, Any]):
    pyjwt, jwt_algorithms, _ = _import_jwt()
    kty = str(jwk.get("kty", "")).upper()
    if kty == "RSA":
        return jwt_algorithms.RSAAlgorithm.from_jwk(jwk)
    if kty == "EC":
        return jwt_algorithms.ECAlgorithm.from_jwk(jwk)
    raise _unauthorized(f"unsupported JWK kty: {kty or '<missing>'}")


def verify_supabase_jwt(
    token: str,
    *,
    cache: Optional[JwksCache] = None,
    audience: Optional[str] = None,
    issuer: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify a Supabase-issued JWT and return its claim dict.

    Raises :class:`AuthError` (401 / 403 / 503) on failure. Never raises
    arbitrary exceptions — callers can trust the status code mapping.
    """
    if not token or not token.strip():
        raise _unauthorized("missing bearer token")
    pyjwt, _, jwt_exceptions = _import_jwt()
    try:
        unverified_header = pyjwt.get_unverified_header(token)
    except jwt_exceptions.PyJWTError as exc:
        raise _unauthorized(f"malformed token: {exc}") from exc

    alg = str(unverified_header.get("alg", ""))
    if alg not in ALLOWED_ALGORITHMS:
        raise _unauthorized(f"disallowed signing algorithm: {alg or '<missing>'}")
    kid = unverified_header.get("kid")
    if not kid:
        raise _unauthorized("token header missing kid")

    jwks = cache or get_default_cache()
    try:
        jwk = jwks.get_jwk(str(kid))
    except JWKSUnavailable as exc:
        raise _jwks_unavailable(str(exc)) from exc

    try:
        signing_key = _signing_key_from_jwk(jwk)
    except AuthError:
        raise

    aud = audience if audience is not None else _supabase_aud()
    iss = issuer if issuer is not None else _supabase_iss()
    decode_kwargs: Dict[str, Any] = {
        "algorithms": [alg],
        "leeway": JWT_CLOCK_SKEW_SECONDS,
        "options": {"require": ["exp", "sub"]},
    }
    if aud:
        decode_kwargs["audience"] = aud
    if iss:
        decode_kwargs["issuer"] = iss

    try:
        claims = pyjwt.decode(token, signing_key, **decode_kwargs)
    except jwt_exceptions.InvalidAudienceError as exc:
        raise _forbidden(f"wrong audience: {exc}") from exc
    except jwt_exceptions.InvalidIssuerError as exc:
        raise _forbidden(f"wrong issuer: {exc}") from exc
    except jwt_exceptions.ExpiredSignatureError as exc:
        raise _unauthorized(f"token expired: {exc}") from exc
    except jwt_exceptions.ImmatureSignatureError as exc:
        raise _unauthorized(f"token not yet valid: {exc}") from exc
    except jwt_exceptions.InvalidSignatureError as exc:
        raise _unauthorized(f"bad signature: {exc}") from exc
    except jwt_exceptions.PyJWTError as exc:
        raise _unauthorized(f"invalid token: {exc}") from exc

    if not claims.get("sub"):
        raise _unauthorized("token missing sub claim")
    return claims


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise _unauthorized("missing bearer token")
    return auth[7:].strip()


def _upsert_founder(db: Session, sub: str, email: Optional[str]) -> Founder:
    """Look up a founder by Supabase ``sub``; lazily create one on first call.

    The Founder row created here is intentionally minimal — full profile
    data arrives later via ``POST /applications``. Subsequent calls are a
    plain SELECT.
    """
    existing = db.query(Founder).filter(Founder.founder_user_id == sub).one_or_none()
    if existing is not None:
        return existing
    founder_id = f"f_{sub[:32]}"
    founder = Founder(
        id=founder_id,
        full_name="",
        email=(email or "").strip() or f"{sub}@unknown.invalid",
        company_name="",
        country="",
        founder_user_id=sub,
    )
    db.add(founder)
    try:
        db.flush()
    except Exception:
        db.rollback()
        existing = (
            db.query(Founder).filter(Founder.founder_user_id == sub).one_or_none()
        )
        if existing is not None:
            return existing
        raise
    return founder


_SERVICE_ROLES = {"admin", "analyst", "viewer"}


def current_founder(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[Founder]:
    """FastAPI dependency: verify the Bearer JWT and return the Founder row.

    Dual-mode by design:

    * **Founder portal** — request carries ``Authorization: Bearer <jwt>``.
      The JWT is verified, the ``sub`` claim is upserted onto
      :class:`Founder`, and the row is returned. Ownership checks in the
      route handler enforce that the caller can only touch their own
      applications.
    * **Service-role bypass** — request carries no Bearer JWT but the
      ``FundSecurityMiddleware`` already authenticated an API-key principal
      with role ``admin``, ``analyst``, or ``viewer``. We return ``None``
      so the route handler can skip ownership checks. This is the path
      that workers and admin tooling take; it bypasses RLS by design.

    The dependency commits the lazy upsert in its own transaction so a
    later route handler error does not orphan the founder identity. Only
    the truncated ``sub`` ever reaches the log line — never the raw
    token or ``Authorization`` header.
    """
    auth = request.headers.get("authorization", "")
    has_bearer = auth.lower().startswith("bearer ")
    if not has_bearer:
        principal = getattr(request.state, "principal", None) or {}
        role = str(principal.get("role", "")).lower()
        if role in _SERVICE_ROLES:
            return None
        raise _unauthorized("missing bearer token")

    token = auth[7:].strip()
    claims = verify_supabase_jwt(token)
    sub = str(claims["sub"])
    email = claims.get("email")
    founder = _upsert_founder(db, sub=sub, email=str(email) if email else None)
    db.commit()
    request.state.principal = {
        "auth_type": "supabase_jwt",
        "role": "founder",
        "founder_id": founder.id,
        "founder_user_id": founder.founder_user_id,
        "fingerprint": f"sub={_truncate_sub(sub)}",
        "key_id": None,
    }
    LOGGER.info("authenticated founder sub=%s", _truncate_sub(sub))
    return founder
