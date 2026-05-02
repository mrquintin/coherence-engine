"""Arq async worker for the fund orchestrator.

Pure-async, Redis-backed worker that runs the same task units as the
legacy polling worker (see :mod:`tasks`). Selected with
``WORKER_BACKEND=arq``.

Run with::

    python -m coherence_engine.server.fund.workers.arq_worker

(or via the Arq CLI: ``arq coherence_engine.server.fund.workers.arq_worker.WorkerSettings``).

This module only imports ``arq`` lazily inside :func:`_worker_settings`
so deployments that stay on ``WORKER_BACKEND=poll`` are not forced to
install the dependency.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict, Optional

from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.observability.otel import (
    get_tracer,
    init_tracing,
    safe_set_attributes,
)
from coherence_engine.server.fund.workers import tasks as _tasks

_LOG = logging.getLogger(__name__)
_TRACER = get_tracer("coherence_engine.fund.workers.arq")


# ---------------------------------------------------------------------------
# Async stubs Arq dispatches to. Each is a thin coroutine wrapper around
# the synchronous pure-function in ``tasks.py``; the synchronous body is
# offloaded with ``asyncio.to_thread`` so the worker event loop stays
# responsive while a job runs.
# ---------------------------------------------------------------------------


async def score_job(ctx: Dict[str, Any], application_id: str) -> Dict[str, Any]:
    """Arq task: run scoring for one application."""
    _LOG.info(
        "arq.score_job.start application_id=%s job_id=%s try=%s",
        application_id,
        ctx.get("job_id"),
        ctx.get("job_try"),
    )
    with _TRACER.start_as_current_span("arq.score_job") as span:
        safe_set_attributes(
            span,
            {
                "arq.task": "score_job",
                "arq.job_id": str(ctx.get("job_id") or ""),
                "arq.job_try": int(ctx.get("job_try") or 0),
                "application.id": str(application_id) if application_id else None,
            },
        )
        result = await asyncio.to_thread(_tasks.run_scoring_job, application_id)
        safe_set_attributes(span, {"arq.result.status": str(result.get("status") or "")})
    _LOG.info(
        "arq.score_job.done application_id=%s status=%s",
        application_id,
        result.get("status"),
    )
    return result


async def dispatch_outbox(ctx: Dict[str, Any], limit: int = 100) -> int:
    """Arq task: dispatch one outbox batch."""
    with _TRACER.start_as_current_span("arq.dispatch_outbox") as span:
        safe_set_attributes(
            span,
            {
                "arq.task": "dispatch_outbox",
                "arq.job_id": str(ctx.get("job_id") or ""),
                "outbox.limit": int(limit),
            },
        )
        published = await asyncio.to_thread(_tasks.dispatch_outbox_batch, limit)
        safe_set_attributes(span, {"outbox.published": int(published or 0)})
        return published


async def run_backtest(ctx: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Arq task: run a deterministic backtest."""
    with _TRACER.start_as_current_span("arq.run_backtest") as span:
        safe_set_attributes(
            span,
            {
                "arq.task": "run_backtest",
                "arq.job_id": str(ctx.get("job_id") or ""),
            },
        )
        return await asyncio.to_thread(_tasks.run_backtest_async, config)


# ---------------------------------------------------------------------------
# Worker lifecycle hooks
# ---------------------------------------------------------------------------


async def on_startup(ctx: Dict[str, Any]) -> None:
    # Initialise OpenTelemetry tracing in the worker process so spans
    # emitted by the scoring service and outbound HTTPX clients are
    # exported. ``init_tracing`` is idempotent and a no-op when the
    # SDK is not installed.
    init_tracing(
        service_name=(settings.OTEL_SERVICE_NAME or settings.SERVICE_NAME) + ".worker",
        environment=settings.environment,
    )
    _LOG.info(
        "arq_worker.startup queue_prefix=%s redis=%s",
        settings.ARQ_QUEUE_PREFIX,
        _redacted_redis_url(settings.REDIS_URL),
    )


async def on_shutdown(ctx: Dict[str, Any]) -> None:
    _LOG.info("arq_worker.shutdown")


async def on_job_start(ctx: Dict[str, Any]) -> None:
    _LOG.debug(
        "arq.job.start name=%s job_id=%s try=%s",
        ctx.get("function_name"),
        ctx.get("job_id"),
        ctx.get("job_try"),
    )


async def on_job_end(ctx: Dict[str, Any]) -> None:
    _LOG.debug(
        "arq.job.end name=%s job_id=%s",
        ctx.get("function_name"),
        ctx.get("job_id"),
    )


def _redacted_redis_url(url: str) -> str:
    """Strip user:pass from a Redis DSN for log lines."""
    if not url or "@" not in url:
        return url
    scheme, _, tail = url.partition("://")
    _, _, host = tail.partition("@")
    return f"{scheme}://***@{host}"


# ---------------------------------------------------------------------------
# WorkerSettings — the entrypoint Arq expects.
# ---------------------------------------------------------------------------


def _redis_settings() -> Any:
    """Build the ``RedisSettings`` block from ``settings.REDIS_URL``."""
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(settings.REDIS_URL)


class WorkerSettings:
    """Arq worker config. See https://arq-docs.helpmanual.io.

    Tunables exposed as class attributes so operators can override them
    via subclassing in deploy environments without editing this module.
    """

    functions = [score_job, dispatch_outbox, run_backtest]
    on_startup = staticmethod(on_startup)
    on_shutdown = staticmethod(on_shutdown)
    on_job_start = staticmethod(on_job_start)
    on_job_end = staticmethod(on_job_end)

    # Concurrency. Arq runs all functions in the same worker; bound it
    # so a CPU-heavy scoring job cannot starve the outbox dispatcher.
    max_jobs = 4
    # 24h result retention. Allows operators to inspect failed-job
    # error context without re-running.
    keep_result = 86400
    max_tries = 3
    job_timeout = 900  # 15 min
    poll_delay = 0.5

    @classmethod
    def get_redis_settings(cls) -> Any:
        return _redis_settings()


# Arq introspects ``redis_settings`` as a class attribute on
# WorkerSettings, so attach it lazily — building it at import time would
# force ``arq`` to be installed even on the poll backend.
def _attach_redis_settings() -> None:
    try:
        WorkerSettings.redis_settings = _redis_settings()  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - import-time-only path
        _LOG.warning(
            "arq_worker: redis_settings not attached at import (%s); "
            "set REDIS_URL and re-import",
            exc,
        )


def main(argv: Optional[list] = None) -> int:
    """Run the Arq worker process.

    Equivalent to ``arq coherence_engine.server.fund.workers
    .arq_worker.WorkerSettings`` but lets us keep a single python -m
    entrypoint that matches the systemd unit.
    """
    from arq.worker import run_worker

    _attach_redis_settings()
    run_worker(WorkerSettings)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main(sys.argv[1:]))
