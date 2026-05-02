"""HMAC request signing for ``/api/v1/internal/*`` (prompt 37).

Service-to-service calls present two extra headers:

- ``X-Coherence-Timestamp`` — RFC 3339 UTC timestamp of the request.
- ``X-Coherence-Signature`` — ``v1=<hex>``, where ``<hex>`` is
  ``HMAC-SHA-256(REQUEST_SIGNING_SECRET, ts \\n method \\n path \\n
  sha256(body))``.

Skew is bounded by ``REQUEST_SIGNING_MAX_SKEW_SECONDS`` (default 300).
A bounded LRU of recently-seen ``(timestamp, signature)`` pairs is used
to reject replays within the skew window.

Required only on ``/api/v1/internal/*`` paths. All other paths skip the
signing check entirely.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from coherence_engine.server.fund.config import settings


_log = logging.getLogger(__name__)


SIGNATURE_HEADER = "X-Coherence-Signature"
TIMESTAMP_HEADER = "X-Coherence-Timestamp"
SIGNATURE_PREFIX = "v1="
INTERNAL_PATH_PREFIX = "/api/v1/internal/"
REPLAY_CACHE_CAPACITY = 10_000


def _truncate_sig_for_log(sig: str) -> str:
    body = sig[len(SIGNATURE_PREFIX):] if sig.startswith(SIGNATURE_PREFIX) else sig
    return f"{SIGNATURE_PREFIX}{body[:8]}..." if body else SIGNATURE_PREFIX


def _parse_rfc3339(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_signature(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    """Build the canonical signature for ``(ts, method, path, body)``.

    Returned without the ``v1=`` prefix — callers that emit a header
    should prepend it.
    """

    body_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = f"{timestamp}\n{method.upper()}\n{path}\n{body_hash}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return digest


class _ReplayCache:
    """Bounded LRU of ``(timestamp, signature)`` pairs."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._seen: "collections.OrderedDict[Tuple[str, str], None]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def seen(self, ts: str, sig: str) -> bool:
        key = (ts, sig)
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return True
            self._seen[key] = None
            if len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


REPLAY_CACHE = _ReplayCache(REPLAY_CACHE_CAPACITY)


def _deny(message: str, sig_header: str) -> JSONResponse:
    _log.info("request signing denied: %s sig=%s", message, _truncate_sig_for_log(sig_header))
    return JSONResponse(
        status_code=401,
        content={"error": "invalid_signature", "message": message},
    )


class RequestSigningMiddleware(BaseHTTPMiddleware):
    """Enforce HMAC-SHA-256 signatures on internal service routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(INTERNAL_PATH_PREFIX):
            return await call_next(request)

        secret = settings.REQUEST_SIGNING_SECRET
        if not secret:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "signing_unconfigured",
                    "message": "request signing required but secret not configured",
                },
            )

        ts_header = request.headers.get(TIMESTAMP_HEADER, "").strip()
        sig_header = request.headers.get(SIGNATURE_HEADER, "").strip()
        if not ts_header or not sig_header:
            return _deny("missing signing headers", sig_header)
        if not sig_header.startswith(SIGNATURE_PREFIX):
            return _deny("unsupported signature version", sig_header)

        ts_dt = _parse_rfc3339(ts_header)
        if ts_dt is None:
            return _deny("invalid timestamp", sig_header)

        now = datetime.now(timezone.utc)
        skew = abs((now - ts_dt).total_seconds())
        if skew > settings.REQUEST_SIGNING_MAX_SKEW_SECONDS:
            return _deny("timestamp outside allowed skew", sig_header)

        body = await request.body()
        expected = compute_signature(secret, ts_header, request.method, path, body)
        provided = sig_header[len(SIGNATURE_PREFIX):]
        if not hmac.compare_digest(expected, provided):
            return _deny("signature mismatch", sig_header)

        if REPLAY_CACHE.seen(ts_header, provided):
            return _deny("replay detected", sig_header)

        # Re-stash the body so downstream handlers can re-read it. We
        # do this by patching ``request._receive`` to replay the body
        # exactly once. Without this, calling ``await request.body()``
        # in the middleware would consume the stream and the route
        # handler would see an empty body.
        async def _replay_receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replay_receive  # type: ignore[attr-defined]
        request.state.signed_request = True
        response: Response = await call_next(request)
        return response


__all__ = [
    "INTERNAL_PATH_PREFIX",
    "REPLAY_CACHE",
    "RequestSigningMiddleware",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "compute_signature",
]
