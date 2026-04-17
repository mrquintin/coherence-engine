"""Production-oriented FastAPI app for fund workflow."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.database import init_db
from coherence_engine.server.fund.routers.admin_api_keys import router as admin_api_keys_router
from coherence_engine.server.fund.routers.admin_ui import router as admin_ui_router
from coherence_engine.server.fund.routers.applications import router as applications_router
from coherence_engine.server.fund.routers.health import router as health_router
from coherence_engine.server.fund.routers.health import set_secret_manager_status
from coherence_engine.server.fund.security import FundSecurityMiddleware
from coherence_engine.server.fund.services.secret_manager import (
    SecretManagerError,
    probe_secret_manager_reachability,
    validate_secret_manager_policy,
)


_ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "static" / "admin"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Coherence Fund Orchestrator API",
        version=settings.SERVICE_VERSION,
        description="Production-ready package layout with persistence and event outbox.",
    )
    app.add_middleware(FundSecurityMiddleware)

    @app.on_event("startup")
    def _startup() -> None:
        if settings.AUTO_CREATE_TABLES:
            init_db()
        try:
            validate_secret_manager_policy()
            probe = probe_secret_manager_reachability(
                os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "")
            )
            set_secret_manager_status(probe)
        except SecretManagerError as exc:
            set_secret_manager_status(
                {
                    "status": "failed",
                    "provider": os.getenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "disabled"),
                    "reachable": False,
                    "detail": str(exc),
                }
            )
            enforce = os.getenv("COHERENCE_FUND_SECRET_MANAGER_STARTUP_ENFORCE", "true").lower() == "true"
            if enforce:
                raise RuntimeError(f"secret manager startup check failed: {exc}") from exc

    # Primary contract-aligned mount (matches docs/specs/openapi_v1.yaml).
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(applications_router, prefix="/api/v1")
    app.include_router(admin_api_keys_router, prefix="/api/v1")

    # Backward-compatible legacy mount.
    app.include_router(health_router)
    app.include_router(applications_router)
    app.include_router(admin_api_keys_router)

    # Read-only admin dashboard (prompt 19). The router already
    # declares ``prefix="/admin"`` and gates every route to the
    # ``admin`` role; static assets (vendored HTMX + admin.css) are
    # served locally from ``server/fund/static/admin`` with no CDN
    # reference. The admin UI performs no writes.
    app.include_router(admin_ui_router)
    if _ADMIN_STATIC_DIR.is_dir():
        app.mount(
            "/admin/static",
            StaticFiles(directory=str(_ADMIN_STATIC_DIR)),
            name="admin-static",
        )
    return app

