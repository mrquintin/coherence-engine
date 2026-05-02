"""Background-worker package for the fund orchestrator.

Contains an Arq-based async worker (:mod:`arq_worker`), the pure-
function task units shared with the legacy polling worker
(:mod:`tasks`), and the enqueue helpers (:mod:`dispatch`) used by
request handlers and CLI entrypoints. The legacy polling worker
remains the failsafe behind ``WORKER_BACKEND=poll``.
"""

from coherence_engine.server.fund.workers.dispatch import (
    enqueue_scoring_job,
    enqueue_outbox_dispatch,
    enqueue_backtest,
)
from coherence_engine.server.fund.workers.tasks import (
    run_scoring_job,
    dispatch_outbox_batch,
    run_backtest_async,
)

__all__ = [
    "enqueue_scoring_job",
    "enqueue_outbox_dispatch",
    "enqueue_backtest",
    "run_scoring_job",
    "dispatch_outbox_batch",
    "run_backtest_async",
]
