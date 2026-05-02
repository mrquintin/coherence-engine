"""Health router.

Liveness (``/healthz``) and readiness (``/readyz``) MUST remain
unauthenticated — Kubernetes probes, load-balancer health checks, and the
Supabase status dashboard do not carry API keys or Supabase JWTs. The
``FundSecurityMiddleware`` short-circuits on non-fund paths, and these
endpoints are deliberately outside the ``/applications`` / ``/admin``
namespaces so they bypass auth without any explicit allowlist edit.

Audit posture
-------------

* ``GET /healthz``   — liveness; anyone (including unauthenticated probes).
* ``GET /readyz``    — readiness (DB + JWKS reachability); anyone.
* ``GET /api/v1/applications/*`` — founder JWT or service-role API key.
* ``GET /admin/*``   — admin API key only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from coherence_engine.server.fund.api_utils import envelope, new_request_id
from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.database import SessionLocal
from coherence_engine.server.fund.security.jwks_cache import get_default_cache

router = APIRouter(tags=["health"])
_secret_manager_status: dict = {
    "status": "unknown",
    "provider": "unknown",
    "reachable": False,
    "detail": "startup probe has not run",
    "checked_at": None,
}


def set_secret_manager_status(status_payload: dict) -> None:
    _secret_manager_status.update(status_payload)
    _secret_manager_status["checked_at"] = datetime.now(timezone.utc).isoformat()


@router.get("/health")
def health(x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id")):
    request_id = x_request_id or new_request_id()
    return envelope(
        request_id=request_id,
        data={
            "status": "ok",
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
        },
    )


@router.get("/live")
def live(x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id")):
    request_id = x_request_id or new_request_id()
    return envelope(
        request_id=request_id,
        data={
            "status": "alive",
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
        },
    )


@router.get("/ready")
def ready(x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id")):
    request_id = x_request_id or new_request_id()
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return envelope(
            request_id=request_id,
            data={
                "status": "ready",
                "database": "ok",
                "service": settings.SERVICE_NAME,
            },
        )
    except SQLAlchemyError as exc:
        payload = envelope(
            request_id=request_id,
            error={
                "code": "NOT_READY",
                "message": "database connectivity check failed",
                "details": [{"reason": str(exc)}],
            },
        )
        return JSONResponse(status_code=503, content=payload)
    finally:
        db.close()


@router.get("/healthz")
def healthz(x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id")):
    """Liveness probe — never authenticated, never blocked by auth.

    Returns ``200`` so long as the process is up. Does not touch the
    database or any external dependency; that's the readiness probe's
    job.
    """
    request_id = x_request_id or new_request_id()
    return envelope(
        request_id=request_id,
        data={
            "status": "alive",
            "service": settings.SERVICE_NAME,
            "version": settings.SERVICE_VERSION,
        },
    )


@router.get("/readyz")
def readyz(x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id")):
    """Readiness probe — checks DB + JWKS reachability, unauthenticated.

    Returns ``503`` if the database is unreachable or the Supabase JWKS
    endpoint cannot be fetched (since founder-portal traffic would fail
    on every request). Returns ``200`` only when both succeed.
    """
    request_id = x_request_id or new_request_id()
    db_ok = True
    db_detail = "ok"
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        db_ok = False
        db_detail = str(exc)
    finally:
        db.close()

    jwks_ok = True
    jwks_detail = "ok"
    try:
        jwks_ok = bool(get_default_cache().is_reachable())
        if not jwks_ok:
            jwks_detail = "unreachable"
    except Exception as exc:  # pragma: no cover - defensive
        jwks_ok = False
        jwks_detail = str(exc)

    overall = "ready" if (db_ok and jwks_ok) else "not_ready"
    payload = envelope(
        request_id=request_id,
        data={
            "status": overall,
            "database": "ok" if db_ok else db_detail,
            "jwks": "ok" if jwks_ok else jwks_detail,
            "service": settings.SERVICE_NAME,
        },
    )
    if not (db_ok and jwks_ok):
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/secret-manager/ready")
def secret_manager_ready(x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id")):
    request_id = x_request_id or new_request_id()
    status = str(_secret_manager_status.get("status", "unknown"))
    payload = envelope(
        request_id=request_id,
        data={
            "status": status,
            "provider": _secret_manager_status.get("provider", "unknown"),
            "reachable": bool(_secret_manager_status.get("reachable", False)),
            "detail": _secret_manager_status.get("detail", ""),
            "checked_at": _secret_manager_status.get("checked_at"),
            "service": settings.SERVICE_NAME,
        },
    )
    if status in {"failed", "unknown"}:
        return JSONResponse(status_code=503, content=payload)
    return payload

