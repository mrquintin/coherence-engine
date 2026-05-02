"""JWKS cache for Supabase JWT verification.

Supabase signs API JWTs with an asymmetric key (RS256 / ES256). The public
verification keys are published as a JWKS document at
``$SUPABASE_URL/auth/v1/.well-known/jwks.json`` (``SUPABASE_JWKS_URL`` here).
This module fetches that document, caches it for ``JWKS_CACHE_TTL_SECONDS``,
and refreshes on a ``kid`` cache-miss so we tolerate key rotation without
requiring a service restart.

Refresh semantics
-----------------

* First request: fetch JWKS, populate cache.
* Subsequent requests: serve from cache until TTL expires.
* ``kid`` cache-miss: trigger a refresh, but rate-limited to one refresh per
  ``kid`` per ``KID_REFRESH_RATE_LIMIT_SECONDS`` so a malicious caller cannot
  force unbounded JWKS fetches by sending random ``kid`` values.
* Network failure with no cached keys: raise :class:`JWKSUnavailable` so the
  auth dependency can map it to a 503 (the service is unable to verify
  *anyone*'s token until JWKS comes back) rather than a 500.

Tests inject keys directly via :meth:`JwksCache.set_key_for_test` so they do
not need to spin up an HTTP fixture for every call.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import httpx

LOGGER = logging.getLogger("coherence_engine.fund.auth.jwks")

JWKS_CACHE_TTL_SECONDS = int(os.getenv("JWKS_CACHE_TTL_SECONDS", "3600"))
KID_REFRESH_RATE_LIMIT_SECONDS = 30
JWKS_FETCH_TIMEOUT_SECONDS = float(os.getenv("JWKS_FETCH_TIMEOUT_SECONDS", "5.0"))


class JWKSUnavailable(Exception):
    """JWKS endpoint unreachable and no cached keys are available."""


class JwksCache:
    def __init__(
        self,
        jwks_url: Optional[str] = None,
        ttl_seconds: int = JWKS_CACHE_TTL_SECONDS,
        rate_limit_seconds: int = KID_REFRESH_RATE_LIMIT_SECONDS,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._jwks_url = jwks_url
        self._ttl_seconds = ttl_seconds
        self._rate_limit_seconds = rate_limit_seconds
        self._http_client = http_client
        self._lock = threading.Lock()
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._fetched_at: float = 0.0
        self._last_refresh_attempt: Dict[str, float] = {}

    @property
    def jwks_url(self) -> str:
        url = self._jwks_url or os.getenv("SUPABASE_JWKS_URL", "").strip()
        if not url:
            raise JWKSUnavailable("SUPABASE_JWKS_URL is not configured")
        return url

    def _is_fresh(self, now: float) -> bool:
        return bool(self._keys) and (now - self._fetched_at) < self._ttl_seconds

    def _can_refresh_for_kid(self, kid: str, now: float) -> bool:
        last = self._last_refresh_attempt.get(kid, 0.0)
        return (now - last) >= self._rate_limit_seconds

    def _do_fetch(self) -> Dict[str, Dict[str, Any]]:
        client = self._http_client
        owns_client = False
        if client is None:
            client = httpx.Client(timeout=JWKS_FETCH_TIMEOUT_SECONDS)
            owns_client = True
        try:
            resp = client.get(self.jwks_url)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JWKSUnavailable(f"failed to fetch JWKS: {exc}") from exc
        finally:
            if owns_client:
                client.close()
        keys: Dict[str, Dict[str, Any]] = {}
        for jwk in payload.get("keys", []) or []:
            kid = jwk.get("kid")
            if kid:
                keys[str(kid)] = dict(jwk)
        return keys

    def _refresh(self) -> None:
        try:
            keys = self._do_fetch()
        except JWKSUnavailable:
            if self._keys:
                LOGGER.warning("JWKS refresh failed; serving stale cache")
                return
            raise
        self._keys = keys
        self._fetched_at = time.monotonic()

    def get_jwk(self, kid: str) -> Dict[str, Any]:
        """Return the JWK dict for ``kid``, refreshing on miss when allowed."""
        now = time.monotonic()
        with self._lock:
            if self._is_fresh(now) and kid in self._keys:
                return self._keys[kid]
            if not self._is_fresh(now) or kid not in self._keys:
                if not self._keys or self._can_refresh_for_kid(kid, now):
                    self._last_refresh_attempt[kid] = now
                    self._refresh()
            if kid not in self._keys:
                raise JWKSUnavailable(f"unknown signing kid: {kid}")
            return self._keys[kid]

    def set_key_for_test(self, kid: str, jwk: Dict[str, Any]) -> None:
        with self._lock:
            self._keys[str(kid)] = dict(jwk)
            self._fetched_at = time.monotonic()

    def clear_for_test(self) -> None:
        with self._lock:
            self._keys = {}
            self._fetched_at = 0.0
            self._last_refresh_attempt = {}

    def is_reachable(self) -> bool:
        """Best-effort readiness probe — returns True if JWKS is fetchable now."""
        try:
            keys = self._do_fetch()
        except JWKSUnavailable:
            return False
        return bool(keys)


_DEFAULT_CACHE: Optional[JwksCache] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_cache() -> JwksCache:
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = JwksCache()
        return _DEFAULT_CACHE


def reset_default_cache_for_tests() -> None:
    global _DEFAULT_CACHE
    with _DEFAULT_LOCK:
        _DEFAULT_CACHE = None
