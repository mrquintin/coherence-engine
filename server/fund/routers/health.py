"""Health router."""

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

