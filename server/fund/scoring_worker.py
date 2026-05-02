"""Scoring queue worker for fund applications."""

from __future__ import annotations

import argparse
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import List

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.config import settings
from coherence_engine.server.fund.database import SessionLocal, retry_transient_db_errors
from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository
from coherence_engine.server.fund.services.application_service import ApplicationService
from coherence_engine.server.fund.services.event_publisher import EventPublisher
# Ops snapshots: optional alert routing via COHERENCE_FUND_OPS_ALERT_* (see alert_routing).
from coherence_engine.server.fund.services.ops_telemetry import (
    WORKER_OPS_SNAPSHOT_MARKER,
    emit_worker_ops_snapshot,
)

_LOG = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _int_env(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def scoring_eligible_clause(now: datetime):
    """Jobs claimable by the scoring worker (mirrors claim_next_scoring_job eligibility)."""
    return and_(
        or_(
            models.ScoringJob.status == "queued",
            and_(
                models.ScoringJob.status == "processing",
                models.ScoringJob.lease_expires_at.is_not(None),
                models.ScoringJob.lease_expires_at < now,
            ),
        ),
        or_(
            models.ScoringJob.next_attempt_at.is_(None),
            models.ScoringJob.next_attempt_at <= now,
        ),
        models.ScoringJob.attempts < models.ScoringJob.max_attempts,
    )


def collect_scoring_ops_metrics(db: Session, now: datetime | None = None) -> dict:
    now = now or _utc_now()
    elig = scoring_eligible_clause(now)
    pending = int(
        db.execute(select(func.count()).select_from(models.ScoringJob).where(elig)).scalar_one()
    )
    oldest_at = db.execute(select(func.min(models.ScoringJob.created_at)).where(elig)).scalar_one_or_none()
    oldest_age_seconds: int | None = None
    if oldest_at is not None:
        if isinstance(oldest_at, datetime) and oldest_at.tzinfo is None:
            oldest_at = oldest_at.replace(tzinfo=timezone.utc)
        delta = now - oldest_at  # type: ignore[operator]
        oldest_age_seconds = max(0, int(delta.total_seconds()))

    failed_dlq = int(
        db.execute(
            select(func.count())
            .select_from(models.ScoringJob)
            .where(models.ScoringJob.status == "failed")
        ).scalar_one()
    )
    in_flight = int(
        db.execute(
            select(func.count())
            .select_from(models.ScoringJob)
            .where(
                models.ScoringJob.status == "processing",
                models.ScoringJob.lease_expires_at.is_not(None),
                models.ScoringJob.lease_expires_at >= now,
            )
        ).scalar_one()
    )
    return {
        "eligible_queue_depth": pending,
        "oldest_eligible_age_seconds": oldest_age_seconds,
        "failed_dlq": failed_dlq,
        "processing_in_flight": in_flight,
    }


def _scoring_warn_tags(metrics: dict) -> List[str]:
    tags: List[str] = []
    depth_w = _int_env("COHERENCE_FUND_SCORING_OPS_QUEUE_WARN_DEPTH", 0)
    if depth_w > 0 and int(metrics.get("eligible_queue_depth", 0)) >= depth_w:
        tags.append("queue_depth")
    age_w = _int_env("COHERENCE_FUND_SCORING_OPS_OLDEST_WARN_SECONDS", 0)
    oldest = metrics.get("oldest_eligible_age_seconds")
    if age_w > 0 and oldest is not None and int(oldest) >= age_w:
        tags.append("oldest_latency")
    fail_w = _int_env("COHERENCE_FUND_SCORING_OPS_FAILED_DLQ_WARN_COUNT", 0)
    if fail_w > 0 and int(metrics.get("failed_dlq", 0)) >= fail_w:
        tags.append("failed_dlq")
    return tags


def emit_scoring_ops_snapshot(db: Session, tick_result: dict | None = None) -> dict:
    metrics = collect_scoring_ops_metrics(db)
    warn_tags = _scoring_warn_tags(metrics)
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "scoring",
        "eligible_queue_depth": metrics["eligible_queue_depth"],
        "oldest_eligible_age_seconds": metrics["oldest_eligible_age_seconds"],
        "failed_dlq": metrics["failed_dlq"],
        "processing_in_flight": metrics["processing_in_flight"],
        "tick": tick_result or {},
        "warn": warn_tags,
    }
    emit_worker_ops_snapshot(_LOG, warn_tags=warn_tags, payload=payload)
    return payload


def _retry_decorator():
    """Build a retry decorator using current settings."""
    return retry_transient_db_errors(
        max_attempts=settings.DB_RETRY_MAX_ATTEMPTS,
        base_delay_ms=settings.DB_RETRY_BASE_DELAY_MS,
        max_delay_ms=settings.DB_RETRY_MAX_DELAY_MS,
        logger=_LOG,
    )


@_retry_decorator()
def claim_next_job(
    repository: ApplicationRepository,
    *,
    worker_id: str = "scoring-worker",
    lease_seconds: int = 120,
):
    """Claim the next eligible scoring job, retrying transient DB errors.

    Wraps :meth:`ApplicationRepository.claim_next_scoring_job`. Logic-bug
    errors (``IntegrityError`` / ``DataError``) are NOT retried.
    """
    return repository.claim_next_scoring_job(
        worker_id=worker_id, lease_seconds=lease_seconds
    )


@_retry_decorator()
def mark_job_completed(repository: ApplicationRepository, job_id: str) -> None:
    """Mark a scoring job completed, retrying transient DB errors."""
    repository.mark_scoring_job_completed(job_id)


def _wrap_repository_with_retry(repo: ApplicationRepository) -> ApplicationRepository:
    """Per-instance monkey-patch so the service's internal job-claim and
    job-finish calls inherit the same retry budget the worker uses.

    The service holds the repository by reference; rebinding the methods
    on the instance is the lightest-touch way to make every call
    originating from the worker pass through the retry decorator without
    spreading the decorator across the service surface.
    """
    deco = _retry_decorator()
    repo.claim_next_scoring_job = deco(repo.claim_next_scoring_job)  # type: ignore[method-assign]
    repo.mark_scoring_job_completed = deco(repo.mark_scoring_job_completed)  # type: ignore[method-assign]
    return repo


def db_healthcheck() -> int:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        print(f"scoring worker healthcheck failed: {exc}", flush=True)
        return 1
    finally:
        db.close()
    return 0


def run_scoring_job(
    application_id: str,
    *,
    worker_id: str | None = None,
    lease_seconds: int = 120,
    retry_base_seconds: int = 5,
    db: Session | None = None,
) -> dict:
    """Process exactly one scoring job for ``application_id``.

    Pure function shared by the polling worker and the Arq worker: both
    call this to do the actual unit of scoring work. Claims the next
    eligible job (the DB lease enforces single-flight), runs scoring,
    writes the decision artifact, and emits outbox events.

    Returns a result dict matching :meth:`ApplicationService
    .process_next_scoring_job` plus an ``application_id`` echo. If no
    job is currently eligible (already processed, or lease held by
    another worker), returns ``{"status": "no_job", "application_id":
    application_id}``.
    """
    owns_session = db is None
    session = db or SessionLocal()
    resolved_worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    try:
        service = ApplicationService(
            repository=_wrap_repository_with_retry(ApplicationRepository(session)),
            events=EventPublisher(session),
        )
        result = service.process_next_scoring_job(
            worker_id=resolved_worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
        )
        if owns_session:
            session.commit()
    finally:
        if owns_session:
            session.close()
    if not result:
        return {"status": "no_job", "application_id": application_id}
    result.setdefault("application_id", application_id)
    return result


def process_once(
    max_jobs: int = 100,
    worker_id: str | None = None,
    lease_seconds: int = 120,
    retry_base_seconds: int = 5,
) -> dict:
    db = SessionLocal()
    processed = 0
    failed = 0
    idle = 0
    resolved_worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    try:
        service = ApplicationService(
            repository=_wrap_repository_with_retry(ApplicationRepository(db)),
            events=EventPublisher(db),
        )
        for _ in range(max_jobs):
            result = service.process_next_scoring_job(
                worker_id=resolved_worker_id,
                lease_seconds=lease_seconds,
                retry_base_seconds=retry_base_seconds,
            )
            if not result:
                idle += 1
                break
            processed += 1
            if result.get("status") in {"failed", "retry_scheduled"}:
                failed += 1
            db.commit()
        tick = {"processed": processed, "failed": failed, "idle": idle}
        emit_scoring_ops_snapshot(db, tick_result=tick)
    finally:
        db.close()
    return {"processed": processed, "failed": failed, "idle": idle}


def run_loop(
    max_jobs_per_tick: int = 100,
    poll_seconds: float = 2.0,
    worker_id: str | None = None,
    lease_seconds: int = 120,
    retry_base_seconds: int = 5,
) -> None:
    while True:
        result = process_once(
            max_jobs=max_jobs_per_tick,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
        )
        if result["processed"] == 0:
            time.sleep(poll_seconds)
        else:
            time.sleep(max(0.1, poll_seconds / 2.0))


def main() -> int:
    parser = argparse.ArgumentParser(prog="coherence-fund-scoring-worker")
    parser.add_argument("--healthcheck", action="store_true", help="Run DB connectivity check and exit")
    parser.add_argument("--run-mode", choices=["once", "loop"], default="once")
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-id", type=str, default=None)
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--retry-base-seconds", type=int, default=5)
    args = parser.parse_args()

    if args.healthcheck:
        return db_healthcheck()

    if args.run_mode == "once":
        result = process_once(
            max_jobs=args.max_jobs,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            retry_base_seconds=args.retry_base_seconds,
        )
        print(
            f"Scoring worker run complete: processed={result['processed']} "
            f"failed={result['failed']} idle={result['idle']}"
        )
    else:
        run_loop(
            max_jobs_per_tick=args.max_jobs,
            poll_seconds=args.poll_seconds,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            retry_base_seconds=args.retry_base_seconds,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
