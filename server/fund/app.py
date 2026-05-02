"""Production-oriented FastAPI app for fund workflow."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.database import init_db
from coherence_engine.server.fund.observability.otel import init_tracing
from coherence_engine.server.fund.routers.admin_api_keys import router as admin_api_keys_router
from coherence_engine.server.fund.routers.admin_ui import router as admin_ui_router
from coherence_engine.server.fund.routers.applications import router as applications_router
from coherence_engine.server.fund.routers.partner_api import router as partner_api_router
from coherence_engine.server.fund.routers.health import router as health_router
from coherence_engine.server.fund.routers.health import set_secret_manager_status
from coherence_engine.server.fund.security import FundSecurityMiddleware
from coherence_engine.server.fund.middleware import install_gateway_middleware
from coherence_engine.server.fund.routers.workflow import router as workflow_router
from coherence_engine.server.fund.routers.worker import router as worker_router
from coherence_engine.server.fund.workers.dispatch import (
    enqueue_backtest as _enqueue_backtest,
    enqueue_outbox_dispatch as _enqueue_outbox_dispatch,
    enqueue_scoring_job as _enqueue_scoring_job,
    reset_arq_pool as _reset_arq_pool,
)
from coherence_engine.server.fund.services.secret_manager import (
    SecretManagerError,
    get_secret_resolver,
    probe_secret_manager_reachability,
    validate_secret_manager_policy,
)
from coherence_engine.server.fund.services.secret_manifest import (
    ManifestError,
    MissingRequiredSecret,
)


_ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "static" / "admin"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Coherence Fund Orchestrator API",
        version=settings.SERVICE_VERSION,
        description="Production-ready package layout with persistence and event outbox.",
    )
    # OpenTelemetry tracing (prompt 61) is wired before any router is
    # mounted so the FastAPI auto-instrumentor can wrap the ASGI stack.
    # When the OTel SDK is not installed this call is a logged no-op.
    init_tracing(
        service_name=settings.OTEL_SERVICE_NAME or settings.SERVICE_NAME,
        environment=settings.environment,
        fastapi_app=app,
    )
    app.add_middleware(FundSecurityMiddleware)
    # API gateway layer (prompt 37): rate limit, signing, CORS,
    # request id. ``install_gateway_middleware`` adds the layers in
    # an order that puts request-id assignment outermost.
    install_gateway_middleware(app)

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

        # Manifest verification (Wave 8, prompt 27). In production env
        # any prod_required secret that fails to resolve aborts boot;
        # other envs produce a status report only.
        target_env = os.getenv("COHERENCE_FUND_ENV", os.getenv("APP_ENV", "development")).strip().lower()
        try:
            resolver = get_secret_resolver()
            report = resolver.verify_manifest(target_env)
        except ManifestError as exc:
            raise RuntimeError(f"secret manifest invalid: {exc}") from exc
        except MissingRequiredSecret:
            # Re-raise: production startup must abort with non-zero exit.
            raise
        else:
            if report.missing_required:
                # Non-prod: log but don't abort.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "secret manifest: %d prod_required entries unresolved (env=%s)",
                    len(report.missing_required),
                    target_env,
                )

    # Primary contract-aligned mount (matches docs/specs/openapi_v1.yaml).
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(applications_router, prefix="/api/v1")
    app.include_router(admin_api_keys_router, prefix="/api/v1")
    app.include_router(workflow_router, prefix="/api/v1")
    app.include_router(worker_router, prefix="/api/v1")

    # Backward-compatible legacy mount.
    app.include_router(health_router)
    app.include_router(applications_router)
    app.include_router(admin_api_keys_router)
    app.include_router(workflow_router)
    app.include_router(worker_router)

    # Partner dashboard JSON API (prompt 35). Mounted on both the
    # versioned and legacy prefixes for parity with the rest of the
    # fund surface; the router declares ``prefix="/partner"`` and
    # gates every endpoint to ``partner`` or ``admin`` via
    # ``require_role``. The legacy HTMX admin dashboard at ``/admin``
    # is preserved as a fallback (prompt 35 prohibition).
    app.include_router(partner_api_router, prefix="/api/v1")
    app.include_router(partner_api_router)

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

    # Expose enqueue helpers on app.state so request handlers can call
    # them via FastAPI's BackgroundTasks (never inline) — request-side
    # Redis hiccups must not block the response. The polling backend
    # makes every helper a no-op, so handlers can call unconditionally.
    app.state.enqueue_scoring_job = _enqueue_scoring_job
    app.state.enqueue_outbox_dispatch = _enqueue_outbox_dispatch
    app.state.enqueue_backtest = _enqueue_backtest
    app.state.worker_backend = settings.WORKER_BACKEND

    @app.on_event("shutdown")
    async def _shutdown_arq_pool() -> None:
        try:
            await _reset_arq_pool()
        except Exception:  # pragma: no cover - defensive
            pass

    return app


app = create_app()
