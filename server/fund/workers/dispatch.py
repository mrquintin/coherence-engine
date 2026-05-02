"""Enqueue helpers used by request handlers and CLI entrypoints.

Behavior depends on ``settings.WORKER_BACKEND``:

* ``poll`` (default) — every helper is a no-op. The polling worker
  discovers eligible work via the database (``ScoringJob`` rows for
  scoring; ``EventOutbox`` rows for outbox dispatch) and there is no
  external broker to enqueue against. Returning ``None`` here means
  request handlers can call enqueue helpers unconditionally without
  branching on the backend.

* ``arq`` — helpers schedule the corresponding async job on the Arq
  Redis queue named by ``settings.ARQ_QUEUE_PREFIX``. Idempotency is
  enforced via Arq's ``_job_id``: the *first* enqueue for a given
  ``idempotency_key`` is scheduled; subsequent calls with the same key
  are deduplicated by Arq itself (the function returns ``None``).

Request handlers MUST call these helpers via FastAPI's
``BackgroundTasks`` (or some other background scheduler) so a Redis
hiccup never blocks an inbound request.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from coherence_engine.server.fund.config import settings

_LOG = logging.getLogger(__name__)


_BACKEND_POLL = "poll"
_BACKEND_ARQ = "arq"


def _backend() -> str:
    return (settings.WORKER_BACKEND or _BACKEND_POLL).strip().lower()


# ---------------------------------------------------------------------------
# Arq pool acquisition
# ---------------------------------------------------------------------------


_pool: Any = None  # arq.connections.ArqRedis when available


async def get_arq_pool() -> Any:
    """Return a process-wide Arq Redis pool.

    Lazy-imports ``arq`` so deployments running ``WORKER_BACKEND=poll``
    never need the dependency installed. Re-uses a single pool across
    the process lifetime.
    """
    global _pool
    if _pool is not None:
        return _pool
    from arq import create_pool
    from arq.connections import RedisSettings

    rs = RedisSettings.from_dsn(settings.REDIS_URL)
    _pool = await create_pool(rs)
    return _pool


async def reset_arq_pool() -> None:
    """Close and clear the cached Arq pool. Test-only utility."""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.aclose()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - best effort
        pass
    _pool = None


# ---------------------------------------------------------------------------
# Enqueue helpers
# ---------------------------------------------------------------------------


def _queue_name(suffix: str) -> str:
    prefix = (settings.ARQ_QUEUE_PREFIX or "coherence_fund").strip()
    return f"{prefix}:{suffix}"


async def enqueue_scoring_job(
    application_id: str,
    *,
    idempotency_key: str,
    pool: Optional[Any] = None,
) -> Optional[str]:
    """Schedule a scoring job for ``application_id``.

    ``idempotency_key`` is required and is passed to Arq as ``_job_id``
    so duplicate enqueues (e.g. an idempotent retry of the same
    POST ``/score`` request) are deduplicated by the queue itself: only
    the first call schedules a job. Returns the Arq job id on success,
    or ``None`` when the call was a no-op (poll backend) or a duplicate.
    """
    if not idempotency_key:
        raise ValueError("idempotency_key is required for enqueue_scoring_job")
    if _backend() != _BACKEND_ARQ:
        _LOG.debug(
            "enqueue_scoring_job skipped (backend=%s) application_id=%s",
            _backend(),
            application_id,
        )
        return None
    arq_pool = pool or await get_arq_pool()
    job_id = f"score:{application_id}:{idempotency_key}"
    job = await arq_pool.enqueue_job(
        "score_job",
        application_id,
        _job_id=job_id,
        _queue_name=_queue_name("scoring"),
    )
    return job.job_id if job is not None else None


async def enqueue_outbox_dispatch(
    *,
    limit: int = 100,
    pool: Optional[Any] = None,
) -> Optional[str]:
    """Schedule one outbox-dispatch tick. No-op on the poll backend."""
    if _backend() != _BACKEND_ARQ:
        return None
    arq_pool = pool or await get_arq_pool()
    job = await arq_pool.enqueue_job(
        "dispatch_outbox",
        limit,
        _queue_name=_queue_name("outbox"),
    )
    return job.job_id if job is not None else None


async def enqueue_backtest(
    config: Dict[str, Any],
    *,
    idempotency_key: str,
    pool: Optional[Any] = None,
) -> Optional[str]:
    """Schedule a deterministic backtest run."""
    if not idempotency_key:
        raise ValueError("idempotency_key is required for enqueue_backtest")
    if _backend() != _BACKEND_ARQ:
        return None
    arq_pool = pool or await get_arq_pool()
    job = await arq_pool.enqueue_job(
        "run_backtest",
        config,
        _job_id=f"backtest:{idempotency_key}",
        _queue_name=_queue_name("backtest"),
    )
    return job.job_id if job is not None else None
