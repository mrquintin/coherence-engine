"""V2 API-key auth: scope enforcement + per-key rate limiting (prompt 28).

Use as FastAPI deps on protected routes:

.. code-block:: python

    @router.post("/scoring/jobs/claim")
    def claim(...,
              key=Depends(require_scopes("worker:claim"))):
        ...

The dep extracts the token from ``X-API-Key`` (or ``Authorization:
Bearer …``), looks up the row by 8-char ``prefix``, Argon2id-verifies
the secret in constant time, and confirms every requested scope is
present on the key. Expiry / revocation / rate-limit failures map to
distinct error codes (``UNAUTHORIZED_EXPIRED``,
``UNAUTHORIZED_REVOKED``, ``RATE_LIMITED``) so callers can telemetry
on them separately from a generic 401.

The rate limiter is a per-process, in-memory token bucket keyed by
``prefix``. Cluster-wide limiting is a gateway concern and is deferred
to the API-gateway prompt.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, List, Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.models import ApiKey
from coherence_engine.server.fund.services.api_key_service import (
    ApiKeyService,
    InvalidKey,
)


# ---------------------------------------------------------------------------
# Token bucket rate limiter (per-process).
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Refill-per-minute token bucket. Capacity equals the per-minute rate."""

    __slots__ = ("capacity", "last_refill", "lock", "refill_per_second", "tokens")

    def __init__(self, refill_per_minute: int) -> None:
        self.capacity = float(max(1, refill_per_minute))
        self.refill_per_second = self.capacity / 60.0
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, amount: float = 1.0) -> bool:
        with self.lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self.last_refill)
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.refill_per_second
            )
            self.last_refill = now
            if self.tokens + 1e-9 >= amount:
                self.tokens -= amount
                return True
            return False


class _RateLimiterRegistry:
    """Process-local registry of one bucket per key ``prefix``."""

    def __init__(self) -> None:
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def check(self, prefix: str, refill_per_minute: int) -> bool:
        with self._lock:
            bucket = self._buckets.get(prefix)
            if bucket is None or bucket.capacity != float(refill_per_minute):
                bucket = _TokenBucket(refill_per_minute)
                self._buckets[prefix] = bucket
        return bucket.consume()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


RATE_LIMITER = _RateLimiterRegistry()


# ---------------------------------------------------------------------------
# Token extraction + resolution.
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> Optional[str]:
    header_key = request.headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
    )


def _forbidden(code: str, message: str, details: Optional[dict] = None) -> HTTPException:
    payload = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=payload)


def resolve_api_key(request: Request, db: Session) -> ApiKey:
    """Authenticate a request and return the matching ``ApiKey`` row.

    Side effects: marks ``last_used_at`` on the row, and consumes one
    token from the per-key rate-limit bucket. On rate-limit exhaustion,
    raises an :class:`HTTPException` with status 429 and code
    ``RATE_LIMITED`` *before* the row is mutated, so a denial does not
    extend the key's apparent activity.
    """

    token = _extract_token(request)
    if not token:
        raise _unauthorized("UNAUTHORIZED", "API key missing")

    svc = ApiKeyService()
    try:
        rec = svc.verify_key(db, token)
    except InvalidKey as exc:
        reason = getattr(exc, "reason", "unknown_key")
        if reason == "expired":
            raise _unauthorized(
                "UNAUTHORIZED_EXPIRED", "API key has expired"
            ) from None
        if reason == "revoked":
            raise _unauthorized(
                "UNAUTHORIZED_REVOKED", "API key has been revoked"
            ) from None
        raise _unauthorized("UNAUTHORIZED", "API key is invalid") from None

    if not RATE_LIMITER.check(rec.prefix, int(rec.rate_limit_per_minute or 60)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMITED",
                "message": "per-key rate limit exceeded",
                "details": {
                    "prefix": rec.prefix,
                    "limit_per_minute": int(rec.rate_limit_per_minute or 60),
                    "scope": "per_process",
                },
            },
        )

    svc.mark_used(db, rec)
    request.state.api_key = rec  # type: ignore[attr-defined]
    request.state.api_key_scopes = _scopes_of(rec)  # type: ignore[attr-defined]
    return rec


def _scopes_of(rec: ApiKey) -> List[str]:
    try:
        scopes = json.loads(rec.scopes_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(scopes, list):
        return []
    return [str(s) for s in scopes]


def require_scopes(*scopes: str) -> Callable:
    """FastAPI dep enforcing that every ``scope`` is present on the resolved key.

    Subset semantics — the key may carry additional scopes not requested
    here. A request that fails authentication returns 401; a request
    that authenticates but lacks a required scope returns 403 with a
    machine-readable list of the missing scopes.
    """

    required = tuple(s.strip() for s in scopes if s and s.strip())

    def _dep(
        request: Request,
        db: Session = Depends(get_db),
    ) -> ApiKey:
        rec = resolve_api_key(request, db)
        held = set(_scopes_of(rec))
        missing = [s for s in required if s not in held]
        if missing:
            raise _forbidden(
                "INSUFFICIENT_SCOPE",
                "API key is missing required scope(s)",
                details={"missing": missing, "required": list(required)},
            )
        return rec

    _dep.__name__ = f"require_scopes({','.join(required) or 'authenticated'})"
    return _dep


__all__ = [
    "RATE_LIMITER",
    "resolve_api_key",
    "require_scopes",
]
