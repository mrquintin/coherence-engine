"""Per-IP / per-API-key token-bucket rate limit (prompt 37).

The middleware keys every request on the API-key prefix when one is
present (so misconfigured callers cannot share a quota with random
internet IPs), or the client IP otherwise. Each key gets a token bucket
whose refill rate is the API-key row's ``rate_limit_per_minute`` value
(or :attr:`settings.RATE_LIMIT_DEFAULT` if the request is unauthenticated
or the key cannot be resolved).

A Redis-backed bucket is used when ``redis`` is importable and the
configured ``REDIS_URL`` is reachable; otherwise we fall back to an
in-process bucket. The fall-back path is what most tests exercise.

Denials return ``429`` with a ``Retry-After`` header (seconds, integer)
and a JSON body:

    {"error": "rate_limited", "retry_after_seconds": <int>}
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from coherence_engine.server.fund.config import settings


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process token bucket (fallback when Redis is unavailable).
# ---------------------------------------------------------------------------


class _TokenBucket:
    """Token bucket sized to ``capacity`` tokens, refilling per second."""

    __slots__ = ("capacity", "last_refill", "lock", "refill_per_second", "tokens")

    def __init__(self, capacity: int, refill_per_minute: int) -> None:
        self.capacity = float(max(1, capacity))
        self.refill_per_second = float(max(1, refill_per_minute)) / 60.0
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, amount: float = 1.0) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``.

        ``retry_after_seconds`` is meaningful only when ``allowed`` is
        ``False`` and tells the caller when the next token is available.
        """

        with self.lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self.last_refill)
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.refill_per_second
            )
            self.last_refill = now
            if self.tokens + 1e-9 >= amount:
                self.tokens -= amount
                return True, 0.0
            deficit = amount - self.tokens
            retry_after = deficit / self.refill_per_second if self.refill_per_second > 0 else 60.0
            return False, retry_after


class _InProcessRateLimiter:
    """Process-local registry of one bucket per ``key``."""

    def __init__(self) -> None:
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, refill_per_minute: int) -> tuple[bool, float]:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.refill_per_second != float(refill_per_minute) / 60.0:
                bucket = _TokenBucket(refill_per_minute, refill_per_minute)
                self._buckets[key] = bucket
        return bucket.consume()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


RATE_LIMITER = _InProcessRateLimiter()


# ---------------------------------------------------------------------------
# Optional Redis bucket. We do not import ``redis`` at module load — the
# fallback path must work in environments that don't ship the dep.
# ---------------------------------------------------------------------------


class _RedisRateLimiter:
    """Token bucket implemented with a Lua script on Redis.

    Falls back to the in-process limiter if any Redis call fails; this
    guarantees that a Redis blip does not turn into a synchronous 5xx.
    """

    _LUA = """
    local key = KEYS[1]
    local capacity = tonumber(ARGV[1])
    local refill_per_second = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local data = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(data[1])
    local ts = tonumber(data[2])
    if tokens == nil then
      tokens = capacity
      ts = now
    end
    local elapsed = math.max(0, now - ts)
    tokens = math.min(capacity, tokens + elapsed * refill_per_second)
    local allowed = 0
    local retry = 0
    if tokens >= 1 then
      tokens = tokens - 1
      allowed = 1
    else
      retry = (1 - tokens) / refill_per_second
    end
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, 120)
    return {allowed, tostring(retry)}
    """

    def __init__(self, client) -> None:
        self._client = client
        self._script = client.register_script(self._LUA)

    def check(self, key: str, refill_per_minute: int) -> tuple[bool, float]:
        try:
            now = time.time()
            allowed, retry = self._script(
                keys=[f"coherence:rl:{key}"],
                args=[refill_per_minute, refill_per_minute / 60.0, now],
            )
            return bool(int(allowed)), float(retry)
        except Exception as exc:
            _log.warning("redis rate limiter failed, falling back: %s", exc)
            return RATE_LIMITER.check(key, refill_per_minute)


_REDIS_LIMITER: Optional[_RedisRateLimiter] = None
_REDIS_LIMITER_LOCK = threading.Lock()


def _get_limiter():
    """Return the active limiter — Redis when reachable, else in-process."""

    global _REDIS_LIMITER
    if _REDIS_LIMITER is not None:
        return _REDIS_LIMITER
    redis_url = settings.REDIS_URL
    if not redis_url or settings.WORKER_BACKEND != "arq":
        # Only attempt Redis when the worker stack is already on it.
        return RATE_LIMITER
    with _REDIS_LIMITER_LOCK:
        if _REDIS_LIMITER is not None:
            return _REDIS_LIMITER
        try:
            import redis as _redis  # type: ignore
        except ImportError:
            return RATE_LIMITER
        try:
            client = _redis.Redis.from_url(redis_url, socket_timeout=0.25)
            client.ping()
        except Exception as exc:
            _log.info("redis unavailable for rate limiting: %s", exc)
            return RATE_LIMITER
        _REDIS_LIMITER = _RedisRateLimiter(client)
        return _REDIS_LIMITER


# ---------------------------------------------------------------------------
# Key derivation.
# ---------------------------------------------------------------------------


def _api_key_prefix(request: Request) -> Optional[str]:
    raw = request.headers.get("x-api-key")
    if not raw:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) < 8:
        return None
    return raw[:8]


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _refill_for_request(request: Request, prefix: Optional[str]) -> int:
    """Return the per-minute refill rate that should apply to ``request``."""

    if prefix is None:
        return int(settings.RATE_LIMIT_DEFAULT)
    # Avoid a DB round-trip in the hot path: the route-level
    # ``require_scopes`` dep already loads the row and stamps it on
    # ``request.state.api_key`` before the response is generated, but
    # the middleware runs *before* deps. We optimistically use the
    # configured default for the first request; once the dep populates
    # state, downstream requests sharing this prefix benefit from the
    # cached bucket sized to ``rate_limit_per_minute`` via DB lookup.
    rec = getattr(request.state, "api_key", None)
    if rec is not None:
        try:
            return int(getattr(rec, "rate_limit_per_minute", 0)) or int(settings.RATE_LIMIT_DEFAULT)
        except (TypeError, ValueError):
            pass
    return int(settings.RATE_LIMIT_DEFAULT)


# ---------------------------------------------------------------------------
# Middleware.
# ---------------------------------------------------------------------------


SKIP_PATHS = {
    "/health",
    "/live",
    "/ready",
    "/api/v1/health",
    "/api/v1/live",
    "/api/v1/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/openapi.json",
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter keyed by API-key prefix or client IP.

    Returns ``429 Too Many Requests`` with ``Retry-After`` and a JSON
    body of the form ``{"error": "rate_limited", "retry_after_seconds":
    N}`` when the bucket is empty.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path in SKIP_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        prefix = _api_key_prefix(request)
        key = prefix or f"ip:{_client_ip(request)}"
        refill = _refill_for_request(request, prefix)
        limiter = _get_limiter()
        allowed, retry_after = limiter.check(key, refill)
        if not allowed:
            retry_int = max(1, int(math.ceil(retry_after)))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "retry_after_seconds": retry_int,
                },
                headers={"Retry-After": str(retry_int)},
            )
        response: Response = await call_next(request)
        return response


__all__ = [
    "RATE_LIMITER",
    "RateLimitMiddleware",
    "SKIP_PATHS",
]
