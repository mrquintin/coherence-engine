"""Explicit CORS allow-list (prompt 37).

The configured ``CORS_ALLOWED_ORIGINS`` is a comma-separated list of
origins. A literal ``*`` is only honoured when ``settings.environment``
is ``dev``; in any other environment a wildcard is rejected at install
time so a misconfigured staging/prod deploy fails loudly at boot rather
than silently dropping the same-origin policy.
"""

from __future__ import annotations

from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from coherence_engine.server.fund.config import settings


ALLOWED_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = [
    "Authorization",
    "Content-Type",
    "X-Request-ID",
    "X-Coherence-Signature",
    "X-Coherence-Timestamp",
    "X-API-Key",
]
EXPOSE_HEADERS = ["X-Request-ID"]


def _parse_allow_origins(raw: str) -> List[str]:
    return [o.strip() for o in (raw or "").split(",") if o.strip()]


def install_cors(app: FastAPI) -> None:
    allow_origins = _parse_allow_origins(settings.CORS_ALLOWED_ORIGINS)
    if "*" in allow_origins and settings.environment != "dev":
        raise RuntimeError(
            "COHERENCE_FUND_CORS_ALLOWED_ORIGINS=* is not allowed outside the "
            "dev environment (current environment=%s)" % settings.environment
        )
    if not allow_origins:
        # No allow-list configured — install a no-op CORS layer so that
        # the middleware stack shape is identical across environments
        # but no origin actually receives ``Access-Control-Allow-Origin``.
        allow_origins = []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSE_HEADERS,
        allow_credentials=True,
        max_age=600,
    )


__all__ = [
    "ALLOWED_HEADERS",
    "ALLOWED_METHODS",
    "EXPOSE_HEADERS",
    "install_cors",
]
