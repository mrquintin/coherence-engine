"""Security middleware, role checks, rate limiting, and audit logging."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from coherence_engine.server.fund.api_utils import envelope
from coherence_engine.server.fund.database import SessionLocal
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.secret_manager import SecretManagerError, get_secret_manager


AUDIT_LOGGER = logging.getLogger("coherence_engine.fund.audit")


def _auth_mode() -> str:
    return os.getenv("COHERENCE_FUND_AUTH_MODE", "db").strip().lower()


def _bootstrap_admin_enabled() -> bool:
    return os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true").strip().lower() == "true"


def _bootstrap_admin_secret_ref() -> str:
    return os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "").strip()


def _bootstrap_cache_seconds() -> int:
    raw = os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_CACHE_SECONDS", "30")
    try:
        return max(5, int(raw))
    except ValueError:
        return 30


def _rate_limit_enabled() -> bool:
    return os.getenv("COHERENCE_FUND_RATE_LIMIT_ENABLED", "true").strip().lower() == "true"


def _rate_limit_requests() -> int:
    return int(os.getenv("COHERENCE_FUND_RATE_LIMIT_REQUESTS", "120"))


def _rate_limit_window_seconds() -> int:
    return int(os.getenv("COHERENCE_FUND_RATE_LIMIT_WINDOW_SECONDS", "60"))


class _RateLimiter:
    """Simple in-memory sliding window limiter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: Dict[str, deque] = {}

    def check(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        window_start = now - window_seconds
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < window_start:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


RATE_LIMITER = _RateLimiter()


class _BootstrapTokenCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at_monotonic: float = 0.0

    def get(self) -> Optional[str]:
        with self._lock:
            now = time.monotonic()
            if now < self._expires_at_monotonic:
                return self._token
            self._token = None
            self._expires_at_monotonic = 0.0
            return None

    def set(self, token: Optional[str], ttl_seconds: int) -> None:
        with self._lock:
            self._token = token
            self._expires_at_monotonic = time.monotonic() + max(1, ttl_seconds)


BOOTSTRAP_TOKEN_CACHE = _BootstrapTokenCache()


def _bootstrap_admin_token() -> Optional[str]:
    if not _bootstrap_admin_enabled():
        return None
    cached = BOOTSTRAP_TOKEN_CACHE.get()
    if cached:
        return cached
    secret_ref = _bootstrap_admin_secret_ref()
    if not secret_ref:
        return None
    try:
        manager = get_secret_manager()
        if manager is None:
            return None
        token = manager.get_secret(secret_ref).strip()
    except SecretManagerError:
        token = None
    BOOTSTRAP_TOKEN_CACHE.set(token, _bootstrap_cache_seconds())
    return token


def _reset_bootstrap_cache_for_tests() -> None:
    BOOTSTRAP_TOKEN_CACHE.set(None, 1)


def _request_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _token_from_request(request: Request) -> Optional[str]:
    header_key = request.headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _token_fingerprint(token: str) -> str:
    return ApiKeyService.fingerprint(token)


def _is_fund_path(path: str) -> bool:
    return (
        path.startswith("/applications")
        or path.startswith("/api/v1/applications")
        or path.startswith("/admin/api-keys")
        or path.startswith("/api/v1/admin/api-keys")
    )


def _is_public_path(path: str) -> bool:
    public = {
        "/health",
        "/live",
        "/ready",
        "/secret-manager/ready",
        "/api/v1/health",
        "/api/v1/live",
        "/api/v1/ready",
        "/api/v1/secret-manager/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/v1/openapi.json",
    }
    return path in public


def audit_log(
    event: str,
    request: Request,
    outcome: str,
    details: Optional[dict] = None,
) -> None:
    payload = {
        "event": event,
        "outcome": outcome,
        "path": request.url.path,
        "method": request.method,
        "ip": _request_ip(request),
        "request_id": request.headers.get("x-request-id", ""),
        "principal": getattr(request.state, "principal", None),
        "details": details or {},
    }
    AUDIT_LOGGER.info(json.dumps(payload, sort_keys=True))
    # Persist audit trail to DB when available.
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        principal = getattr(request.state, "principal", None) or {}
        key_id = principal.get("key_id")
        actor = principal.get("fingerprint") or principal.get("role") or ""
        repo.add_audit_event(
            action=event,
            success=(outcome == "allowed"),
            actor=str(actor),
            request_id=request.headers.get("x-request-id", ""),
            ip=_request_ip(request),
            path=request.url.path,
            details=details or {},
            api_key_id=str(key_id) if key_id else None,
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _error_json(request: Request, status_code: int, code: str, message: str, details: Optional[list] = None) -> JSONResponse:
    request_id = request.headers.get("x-request-id", "")
    payload = envelope(
        request_id=request_id or "req_security",
        error={"code": code, "message": message, "details": details or []},
    )
    return JSONResponse(status_code=status_code, content=payload)


class FundSecurityMiddleware(BaseHTTPMiddleware):
    """Applies auth + rate limiting for fund routes."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public_path(path):
            return await call_next(request)
        if not _is_fund_path(path):
            return await call_next(request)

        if _auth_mode() != "disabled":
            token = _token_from_request(request)
            if not token:
                audit_log(
                    event="auth_failure",
                    request=request,
                    outcome="denied",
                    details={"reason": "missing_token"},
                )
                return _error_json(request, 401, "UNAUTHORIZED", "invalid or missing API token")
            principal = None
            if _auth_mode() == "db":
                db = SessionLocal()
                try:
                    repo = ApiKeyRepository(db)
                    svc = ApiKeyService()
                    verification = svc.verify_token(repo, token)
                    if verification.get("ok"):
                        principal = {
                            "auth_type": "api_key_db",
                            "token_fingerprint": verification["fingerprint"],
                            "fingerprint": verification["fingerprint"],
                            "role": verification["role"],
                            "key_id": verification["key_id"],
                        }
                        db.commit()
                    else:
                        db.rollback()
                finally:
                    db.close()

            # Secret-manager bootstrap admin token fallback.
            if principal is None:
                bootstrap = _bootstrap_admin_token()
                if bootstrap and secrets.compare_digest(token, bootstrap):
                    principal = {
                        "auth_type": "secret_manager_bootstrap",
                        "token_fingerprint": _token_fingerprint(token),
                        "fingerprint": _token_fingerprint(token),
                        "role": "admin",
                        "key_id": None,
                    }

            if principal is None:
                audit_log(
                    event="auth_failure",
                    request=request,
                    outcome="denied",
                    details={"reason": "invalid_or_expired_or_revoked_token"},
                )
                return _error_json(request, 401, "UNAUTHORIZED", "invalid, expired, or revoked API token")
            request.state.principal = principal
        else:
            request.state.principal = {
                "auth_type": "disabled",
                "role": "admin",
                "fingerprint": "disabled",
                "key_id": None,
            }

        if _rate_limit_enabled():
            key = f"{request.state.principal.get('fingerprint','anon')}:{path}:{request.method}"
            ok = RATE_LIMITER.check(key, _rate_limit_requests(), _rate_limit_window_seconds())
            if not ok:
                audit_log(
                    event="rate_limit",
                    request=request,
                    outcome="denied",
                    details={"limit": _rate_limit_requests(), "window_seconds": _rate_limit_window_seconds()},
                )
                return _error_json(request, 429, "RATE_LIMITED", "request rate limit exceeded")

        return await call_next(request)


def enforce_roles(request: Request, allowed_roles: Tuple[str, ...]) -> Optional[JSONResponse]:
    principal = getattr(request.state, "principal", None) or {}
    role = str(principal.get("role", "")).lower()
    if role not in {r.lower() for r in allowed_roles}:
        audit_log(
            event="authorization_failure",
            request=request,
            outcome="denied",
            details={"required_roles": list(allowed_roles), "actual_role": role},
        )
        return _error_json(request, 403, "FORBIDDEN", "insufficient role permissions")
    return None

