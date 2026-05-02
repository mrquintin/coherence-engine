"""``X-Request-ID`` middleware: read or mint, attach to logging, echo.

If the caller supplies an ``X-Request-ID`` header we trust it (so a
gateway-side request id flows through to backend logs); otherwise we
mint a UUID4 — the runtime does not yet have a UUIDv7 helper, but the
property we actually need (uniqueness across a request lifetime) is the
same. The id is exposed on ``request.state.request_id`` for handlers
and on every log record emitted within the request via a
``ContextVar``-backed logging filter.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


REQUEST_ID_HEADER = "X-Request-ID"

_REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "coherence_engine_request_id", default=""
)


def current_request_id() -> str:
    """Return the request-id for the current async/threaded context."""

    return _REQUEST_ID_CTX.get()


class RequestIdLogFilter(logging.Filter):
    """Attaches ``record.request_id`` so log formatters can reference it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID_CTX.get() or "-"
        return True


def _new_request_id() -> str:
    return uuid.uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming or _new_request_id()
        token = _REQUEST_ID_CTX.set(request_id)
        request.state.request_id = request_id
        try:
            response: Response = await call_next(request)
        finally:
            _REQUEST_ID_CTX.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIdMiddleware",
    "RequestIdLogFilter",
    "current_request_id",
]
