"""Repository methods for event outbox dispatch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class OutboxRepository:
    """Outbox persistence and status transitions."""

    def __init__(self, db: Session, max_attempts: int = 5, retry_base_seconds: int = 2):
        self.db = db
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))

    def fetch_pending(self, batch_size: int = 100) -> List[models.EventOutbox]:
        now = _utc_now()
        stmt = (
            select(models.EventOutbox)
            .where(models.EventOutbox.status == "pending")
            .where(
                or_(
                    models.EventOutbox.next_attempt_at.is_(None),
                    models.EventOutbox.next_attempt_at <= now,
                )
            )
            .order_by(models.EventOutbox.occurred_at.asc())
            .limit(batch_size)
        )
        return list(self.db.execute(stmt).scalars().all())

    def mark_published(self, event: models.EventOutbox) -> None:
        event.status = "published"
        event.last_error = ""
        event.next_attempt_at = None
        event.published_at = _utc_now()
        self.db.flush()

    def mark_failed(self, event: models.EventOutbox, error_message: str) -> None:
        event.attempts = int(event.attempts or 0) + 1
        event.last_error = error_message[:4000]
        if event.attempts >= self.max_attempts:
            event.status = "failed"
            event.next_attempt_at = None
        else:
            backoff_seconds = self.retry_base_seconds * (2 ** (event.attempts - 1))
            event.status = "pending"
            event.next_attempt_at = _utc_now() + timedelta(seconds=backoff_seconds)
        self.db.flush()

    def list_failed(self, limit: int = 100) -> List[models.EventOutbox]:
        stmt = (
            select(models.EventOutbox)
            .where(models.EventOutbox.status == "failed")
            .order_by(models.EventOutbox.occurred_at.asc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def _pending_dispatchable_clause(self, now: datetime):
        """Rows eligible for dispatch (matches fetch_pending)."""
        return and_(
            models.EventOutbox.status == "pending",
            or_(
                models.EventOutbox.next_attempt_at.is_(None),
                models.EventOutbox.next_attempt_at <= now,
            ),
        )

    def get_ops_metrics(self, now: datetime | None = None) -> dict:
        """Queue depth, oldest pending age, and dead-letter counts for worker ops telemetry."""
        now = now or _utc_now()
        clause = self._pending_dispatchable_clause(now)
        pending_stmt = select(func.count()).select_from(models.EventOutbox).where(clause)
        pending = int(self.db.execute(pending_stmt).scalar_one())

        oldest_stmt = select(func.min(models.EventOutbox.occurred_at)).where(clause)
        oldest_at = self.db.execute(oldest_stmt).scalar_one_or_none()
        oldest_age_seconds: int | None = None
        if oldest_at is not None:
            if isinstance(oldest_at, datetime) and oldest_at.tzinfo is None:
                oldest_at = oldest_at.replace(tzinfo=timezone.utc)
            delta = now - oldest_at  # type: ignore[operator]
            oldest_age_seconds = max(0, int(delta.total_seconds()))

        failed_stmt = select(func.count()).select_from(models.EventOutbox).where(
            models.EventOutbox.status == "failed"
        )
        failed_dlq = int(self.db.execute(failed_stmt).scalar_one())

        return {
            "pending_dispatchable": pending,
            "oldest_pending_age_seconds": oldest_age_seconds,
            "failed_dlq": failed_dlq,
        }

    def replay_failed(self, event_ids: List[str] | None = None, limit: int = 100, reset_attempts: bool = False) -> int:
        stmt = select(models.EventOutbox).where(models.EventOutbox.status == "failed")
        if event_ids:
            stmt = stmt.where(models.EventOutbox.id.in_(event_ids))
        else:
            stmt = stmt.limit(limit)
        rows = list(self.db.execute(stmt).scalars().all())
        for row in rows:
            row.status = "pending"
            row.last_error = ""
            row.next_attempt_at = _utc_now()
            row.published_at = None
            if reset_attempts:
                row.attempts = 0
        self.db.flush()
        return len(rows)

