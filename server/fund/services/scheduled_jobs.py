"""Cron-style scheduled jobs (prompt 55 et al.).

Lightweight registry of long-running, time-triggered jobs. The
runtime invocation contract is:

* Each job is a zero-or-one-arg callable taking an open
  :class:`Session` and returning a small JSON-serializable summary
  dict.
* The scheduler driver (Celery beat / APScheduler / cron) opens a
  session, calls the job, and commits.
* Jobs MUST be idempotent under repeat invocation; the daily window
  reconcilers query a trailing-window state and emit
  ``*_reconciliation_completed`` events.

Currently registered:

* :func:`crm_daily_reconciliation` -- runs once per day, walks each
  configured CRM backend, and applies any missed inbound updates via
  :func:`reconcile_crm_deltas`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund.services.crm_backends import (
    AffinityBackend,
    CRMBackend,
    CRMConfigError,
    HubSpotBackend,
)
from coherence_engine.server.fund.services.crm_sync import (
    ReconciliationResult,
    reconcile_crm_deltas,
)


__all__ = [
    "ScheduledJob",
    "JOBS",
    "crm_daily_reconciliation",
    "build_default_crm_backends",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledJob:
    """Static metadata for a registered scheduled job."""

    name: str
    cron: str  # documented schedule, interpreted by the driver
    handler: Callable[[Session], Dict[str, Any]]
    description: str = ""


def build_default_crm_backends() -> List[CRMBackend]:
    """Construct CRM backends from environment configuration.

    Skips any backend whose required env vars are missing rather than
    raising; the caller may want to run reconciliation against just
    one provider.
    """
    backends: List[CRMBackend] = []
    try:
        backends.append(AffinityBackend.from_env())
    except CRMConfigError as exc:
        _LOG.info("affinity_backend_skipped reason=%s", exc)
    try:
        backends.append(HubSpotBackend.from_env())
    except CRMConfigError as exc:
        _LOG.info("hubspot_backend_skipped reason=%s", exc)
    return backends


def crm_daily_reconciliation(
    db: Session,
    *,
    backends: Optional[List[CRMBackend]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run :func:`reconcile_crm_deltas` once for each CRM backend.

    Returns a summary dict suitable for logging / pager-emit. The
    underlying call emits a ``crm_reconciliation_completed`` event per
    backend so downstream observability picks up the run without
    parsing this return value.
    """
    chosen: List[CRMBackend] = (
        backends if backends is not None else build_default_crm_backends()
    )
    results: Dict[str, Any] = {
        "ran_at": (now or datetime.now(tz=timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "backends": [],
    }
    for backend in chosen:
        outcome: ReconciliationResult = reconcile_crm_deltas(
            db, backend, now=now
        )
        results["backends"].append(
            {
                "provider": getattr(backend, "name", ""),
                "applied": outcome.applied,
                "skipped_already_applied": outcome.skipped_already_applied,
                "unresolved": outcome.unresolved,
                "window_started_at": outcome.window_started_at,
                "window_ended_at": outcome.window_ended_at,
            }
        )
    db.flush()
    return results


JOBS: List[ScheduledJob] = [
    ScheduledJob(
        name="crm_daily_reconciliation",
        cron="0 7 * * *",  # 07:00 UTC daily
        handler=crm_daily_reconciliation,
        description=(
            "Pull last 24h of CRM deltas (Affinity + HubSpot) and "
            "apply any updates the webhook listener missed."
        ),
    ),
]
