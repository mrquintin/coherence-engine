"""Outbox dispatcher worker."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from coherence_engine.server.fund.repositories.outbox_repository import OutboxRepository
# Ops snapshots: optional alert routing via COHERENCE_FUND_OPS_ALERT_* (see alert_routing).
from coherence_engine.server.fund.services.ops_telemetry import (
    WORKER_OPS_SNAPSHOT_MARKER,
    emit_worker_ops_snapshot,
)
from coherence_engine.server.fund.services.outbox_publishers import Publisher

_LOG = logging.getLogger(__name__)


def _int_env(name: str, default: int = 0) -> int:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _outbox_warn_tags(metrics: dict) -> List[str]:
    tags: List[str] = []
    depth_w = _int_env("COHERENCE_FUND_OUTBOX_OPS_QUEUE_WARN_DEPTH", 0)
    if depth_w > 0 and int(metrics.get("pending_dispatchable", 0)) >= depth_w:
        tags.append("queue_depth")
    age_w = _int_env("COHERENCE_FUND_OUTBOX_OPS_OLDEST_WARN_SECONDS", 0)
    oldest = metrics.get("oldest_pending_age_seconds")
    if age_w > 0 and oldest is not None and int(oldest) >= age_w:
        tags.append("oldest_latency")
    fail_w = _int_env("COHERENCE_FUND_OUTBOX_OPS_FAILED_DLQ_WARN_COUNT", 0)
    if fail_w > 0 and int(metrics.get("failed_dlq", 0)) >= fail_w:
        tags.append("failed_dlq")
    return tags


def emit_outbox_ops_snapshot(repo: OutboxRepository, tick_result: Dict[str, int] | None = None) -> None:
    """Emit a single-line JSON snapshot for log scrapers (no external services)."""
    metrics = repo.get_ops_metrics()
    warn_tags = _outbox_warn_tags(metrics)
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "outbox",
        "pending_dispatchable": metrics["pending_dispatchable"],
        "oldest_pending_age_seconds": metrics["oldest_pending_age_seconds"],
        "failed_dlq": metrics["failed_dlq"],
        "tick": tick_result or {},
        "warn": warn_tags,
    }
    emit_worker_ops_snapshot(_LOG, warn_tags=warn_tags, payload=payload)


class OutboxDispatcher:
    """Dispatches pending outbox rows to configured transport."""

    def __init__(
        self,
        db: Session,
        publisher: Publisher,
        topic_prefix: str = "coherence.fund",
        max_attempts: int = 5,
        retry_base_seconds: int = 2,
    ):
        self.db = db
        self.publisher = publisher
        self.topic_prefix = topic_prefix
        self.repo = OutboxRepository(
            db,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
        )

    def _to_envelope(self, event_row) -> Dict[str, object]:
        payload = json.loads(event_row.payload_json)
        occurred = event_row.occurred_at
        if isinstance(occurred, datetime):
            occurred_at = occurred.isoformat()
        else:
            occurred_at = str(occurred)
        return {
            "event_id": event_row.id,
            "event_type": event_row.event_type,
            "event_version": event_row.event_version,
            "occurred_at": occurred_at,
            "producer": event_row.producer,
            "trace_id": event_row.trace_id,
            "idempotency_key": event_row.idempotency_key,
            "payload": payload,
        }

    def dispatch_once(self, batch_size: int = 100) -> Dict[str, int]:
        pending = self.repo.fetch_pending(batch_size=batch_size)
        published = 0
        failed = 0
        for event in pending:
            topic = f"{self.topic_prefix}.{event.event_type}"
            key = event.id
            envelope = self._to_envelope(event)
            try:
                self.publisher.publish(topic=topic, key=key, payload=envelope)
                self.repo.mark_published(event)
                published += 1
            except Exception as exc:  # pragma: no cover - depends on external brokers
                self.repo.mark_failed(event, str(exc))
                failed += 1
        self.db.commit()
        result = {"published": published, "failed": failed, "scanned": len(pending)}
        emit_outbox_ops_snapshot(self.repo, tick_result=result)
        return result


def run_loop(dispatcher: OutboxDispatcher, poll_seconds: float = 2.0, batch_size: int = 100) -> None:
    """Continuously dispatch pending outbox events."""
    while True:
        result = dispatcher.dispatch_once(batch_size=batch_size)
        # Quiet default loop; enable logging through external process manager if desired.
        if result["scanned"] == 0:
            time.sleep(poll_seconds)
        else:
            time.sleep(max(0.1, poll_seconds / 2.0))


def topic_prefix_from_env() -> str:
    return os.getenv("COHERENCE_FUND_TOPIC_PREFIX", "coherence.fund")

