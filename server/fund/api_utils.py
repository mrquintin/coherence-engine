"""Shared API helpers."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi.responses import JSONResponse


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


def envelope(request_id: str, data: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "data": data,
        "error": error,
        "meta": {"request_id": request_id},
    }


def error_response(
    request_id: str,
    status_code: int,
    code: str,
    message: str,
    details: Optional[list] = None,
) -> JSONResponse:
    payload = envelope(
        request_id=request_id,
        error={"code": code, "message": message, "details": details or []},
    )
    return JSONResponse(status_code=status_code, content=payload)

