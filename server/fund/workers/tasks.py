"""Pure-function task units invoked by both worker backends.

Both the legacy polling worker (``WORKER_BACKEND=poll``) and the Arq
async worker (``WORKER_BACKEND=arq``) call into the same callables
defined here. No new business logic lives in this module; it is a
thin adapter that wraps existing services so the worker layer is
trivially substitutable.

The Arq side (see :mod:`arq_worker`) wraps these in async stubs because
Arq's worker runtime expects coroutines; the polling side calls them
synchronously.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from coherence_engine.server.fund.database import SessionLocal
from coherence_engine.server.fund.scoring_worker import (
    run_scoring_job as _run_scoring_job_impl,
)
from coherence_engine.server.fund.services.outbox_dispatcher import OutboxDispatcher
from coherence_engine.server.fund.services.outbox_publishers import (
    KafkaPublisher,
    RedisPublisher,
    SQSPublisher,
)

_LOG = logging.getLogger(__name__)


def run_scoring_job(
    application_id: str,
    *,
    worker_id: Optional[str] = None,
    lease_seconds: int = 120,
    retry_base_seconds: int = 5,
) -> Dict[str, Any]:
    """Run scoring for one application end-to-end.

    Delegates to :func:`coherence_engine.server.fund.scoring_worker
    .run_scoring_job`. Returns a result dict containing at minimum a
    ``status`` field (``completed``, ``failed``, ``retry_scheduled``,
    or ``no_job``).
    """
    return _run_scoring_job_impl(
        application_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        retry_base_seconds=retry_base_seconds,
    )


def _build_outbox_publisher(backend: str, **kwargs: Any):
    backend = (backend or "redis").strip().lower()
    if backend == "redis":
        url = kwargs.get("redis_url") or ""
        if not url:
            raise ValueError("redis_url is required for redis outbox publisher")
        return RedisPublisher(redis_url=url)
    if backend == "kafka":
        servers = kwargs.get("kafka_bootstrap_servers") or ""
        if not servers:
            raise ValueError("kafka_bootstrap_servers is required for kafka publisher")
        return KafkaPublisher(bootstrap_servers=servers)
    if backend == "sqs":
        queue_url = kwargs.get("sqs_queue_url") or ""
        if not queue_url:
            raise ValueError("sqs_queue_url is required for sqs publisher")
        return SQSPublisher(
            queue_url=queue_url, region_name=kwargs.get("sqs_region", "us-east-1")
        )
    raise ValueError(f"Unsupported outbox publisher backend: {backend}")


def dispatch_outbox_batch(
    limit: int = 100,
    *,
    publisher_backend: str = "redis",
    publisher_kwargs: Optional[Dict[str, Any]] = None,
    topic_prefix: str = "coherence.fund",
) -> int:
    """Dispatch up to ``limit`` pending outbox rows to the broker.

    Returns the number of events successfully published. Failed rows
    are marked failed (with retry scheduling) by
    :class:`OutboxDispatcher` and are not counted in the return value.
    """
    publisher = _build_outbox_publisher(
        publisher_backend, **(publisher_kwargs or {})
    )
    db = SessionLocal()
    try:
        dispatcher = OutboxDispatcher(
            db=db, publisher=publisher, topic_prefix=topic_prefix
        )
        result = dispatcher.dispatch_once(batch_size=limit)
        return int(result.get("published", 0))
    finally:
        db.close()


def run_backtest_async(config: Dict[str, Any]) -> Dict[str, Any]:
    """Run a deterministic backtest from a serialized config dict.

    The config dict is the same shape consumed by
    :class:`coherence_engine.server.fund.services.backtest.BacktestConfig`.
    Returns a ``ReportRef`` dict with the report digest and the on-disk
    path the report was written to.
    """
    from coherence_engine.server.fund.services.backtest import (
        BacktestConfig,
        run_backtest,
    )

    cfg = BacktestConfig(
        dataset_path=config["dataset_path"],
        decision_policy_version=config["decision_policy_version"],
        portfolio_snapshot_path=config["portfolio_snapshot_path"],
    )
    report = run_backtest(cfg)
    return {
        "schema_version": getattr(report, "schema_version", ""),
        "row_count": getattr(report, "n_rows", 0),
        "report_digest": report.report_digest(),
    }
