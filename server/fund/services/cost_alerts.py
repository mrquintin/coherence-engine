"""Cost-budget alerts (prompt 62).

Two evaluation entry points:

* :func:`check_application_budget` -- compares the rolled-up
  ``total_usd`` for one application against
  ``MAX_COST_PER_APPLICATION_USD``.
* :func:`check_daily_budget` -- compares the rolled-up ``total_usd``
  for the current UTC day against ``MAX_COST_PER_DAY_USD``.

Both write a ``cost_budget_exceeded.v1`` outbox event when the
threshold is crossed AND the cooldown for that ``(scope, scope_key)``
pair has elapsed. The cooldown ledger lives in
:class:`models.CostAlertState` -- one row per scope+key pair, updated
on every emitted alert. The cooldown defaults to 24h (per prompt 62
prohibition: never alert without cooldown).

The actual notification routing reuses the prompt 14 notifications
service via the outbox dispatcher; this module only deposits the
canonical event.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.cost_telemetry import (
    sum_application_total_usd,
    sum_daily_total_usd,
)
from coherence_engine.server.fund.services.event_publisher import EventPublisher


__all__ = [
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_MAX_COST_PER_APPLICATION_USD",
    "DEFAULT_MAX_COST_PER_DAY_USD",
    "EVENT_COST_BUDGET_EXCEEDED",
    "AlertDecision",
    "check_application_budget",
    "check_daily_budget",
]


_LOG = logging.getLogger("coherence_engine.fund.cost_alerts")


EVENT_COST_BUDGET_EXCEEDED = "cost_budget_exceeded"

SCOPE_APPLICATION = "application"
SCOPE_DAILY = "daily"

DEFAULT_COOLDOWN_HOURS = 24.0
DEFAULT_MAX_COST_PER_APPLICATION_USD = 50.0
DEFAULT_MAX_COST_PER_DAY_USD = 500.0


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _max_per_application() -> float:
    return _env_float(
        "MAX_COST_PER_APPLICATION_USD", DEFAULT_MAX_COST_PER_APPLICATION_USD
    )


def _max_per_day() -> float:
    return _env_float(
        "MAX_COST_PER_DAY_USD", DEFAULT_MAX_COST_PER_DAY_USD
    )


def _cooldown_hours() -> float:
    return _env_float("COST_ALERT_COOLDOWN_HOURS", DEFAULT_COOLDOWN_HOURS)


@dataclass(frozen=True)
class AlertDecision:
    """Result of a budget check.

    * ``exceeded`` -- True iff ``total_usd > budget_usd``.
    * ``alert_emitted`` -- True iff a ``cost_budget_exceeded`` event
      was just written (False on a cooldown-suppressed firing).
    * ``cooldown_active`` -- True iff the cooldown ledger blocked the
      emit. Useful for tests that want to distinguish "first alert,
      now in cooldown" from "still in cooldown".
    """

    scope: str
    scope_key: str
    total_usd: float
    budget_usd: float
    exceeded: bool
    alert_emitted: bool
    cooldown_active: bool
    event_id: Optional[str] = None


def _load_alert_state(
    db: Session,
    *,
    scope: str,
    scope_key: str,
) -> Optional[models.CostAlertState]:
    return (
        db.query(models.CostAlertState)
        .filter(models.CostAlertState.scope == scope)
        .filter(models.CostAlertState.scope_key == scope_key)
        .one_or_none()
    )


def _record_alert_state(
    db: Session,
    *,
    scope: str,
    scope_key: str,
    total_usd: float,
    when: datetime,
) -> None:
    existing = _load_alert_state(db, scope=scope, scope_key=scope_key)
    if existing is None:
        db.add(
            models.CostAlertState(
                id=f"clt_{uuid.uuid4().hex[:16]}",
                scope=scope,
                scope_key=scope_key,
                last_alert_at=when,
                last_total_usd=float(total_usd),
            )
        )
    else:
        existing.last_alert_at = when
        existing.last_total_usd = float(total_usd)
    db.flush()


def _emit_event(
    db: Session,
    *,
    scope: str,
    scope_key: str,
    total_usd: float,
    budget_usd: float,
    cooldown_hours: float,
    publisher: Optional[EventPublisher],
    when: datetime,
) -> Optional[str]:
    pub = publisher or EventPublisher(db)
    payload = {
        "scope": scope,
        "scope_key": scope_key,
        "budget_usd": round(float(budget_usd), 6),
        "total_usd": round(float(total_usd), 6),
        "cooldown_hours": round(float(cooldown_hours), 6),
    }
    pub._validate_external_schema(EVENT_COST_BUDGET_EXCEEDED, payload)
    idempotency_key = (
        f"cost_budget_exceeded:{scope}:{scope_key}:"
        f"{when.strftime('%Y%m%dT%H%M%S')}"
    )
    result = pub.publish(
        event_type=EVENT_COST_BUDGET_EXCEEDED,
        producer="cost_alerts",
        trace_id=f"cost_alert_{uuid.uuid4().hex[:12]}",
        idempotency_key=idempotency_key,
        payload=payload,
    )
    return result.get("event_id")


def _evaluate(
    db: Session,
    *,
    scope: str,
    scope_key: str,
    total_usd: float,
    budget_usd: float,
    publisher: Optional[EventPublisher],
) -> AlertDecision:
    if budget_usd <= 0:
        return AlertDecision(
            scope=scope,
            scope_key=scope_key,
            total_usd=total_usd,
            budget_usd=budget_usd,
            exceeded=False,
            alert_emitted=False,
            cooldown_active=False,
        )

    exceeded = total_usd > budget_usd
    if not exceeded:
        return AlertDecision(
            scope=scope,
            scope_key=scope_key,
            total_usd=total_usd,
            budget_usd=budget_usd,
            exceeded=False,
            alert_emitted=False,
            cooldown_active=False,
        )

    cooldown_hours = _cooldown_hours()
    now = _utc_now()
    state = _load_alert_state(db, scope=scope, scope_key=scope_key)
    if state is not None and cooldown_hours > 0:
        # SQLite hands naive datetimes back even when written as
        # tz-aware -- coerce to UTC before subtracting so the cooldown
        # arithmetic is portable across SQLite (tests) and Postgres
        # (production).
        last_alert = state.last_alert_at
        if last_alert.tzinfo is None:
            last_alert = last_alert.replace(tzinfo=timezone.utc)
        elapsed = now - last_alert
        if elapsed < timedelta(hours=cooldown_hours):
            _LOG.info(
                "cost_alert_cooldown_active scope=%s key=%s elapsed_h=%.2f",
                scope,
                scope_key,
                elapsed.total_seconds() / 3600.0,
            )
            return AlertDecision(
                scope=scope,
                scope_key=scope_key,
                total_usd=total_usd,
                budget_usd=budget_usd,
                exceeded=True,
                alert_emitted=False,
                cooldown_active=True,
            )

    event_id = _emit_event(
        db,
        scope=scope,
        scope_key=scope_key,
        total_usd=total_usd,
        budget_usd=budget_usd,
        cooldown_hours=cooldown_hours,
        publisher=publisher,
        when=now,
    )
    _record_alert_state(
        db,
        scope=scope,
        scope_key=scope_key,
        total_usd=total_usd,
        when=now,
    )
    _LOG.warning(
        "cost_budget_exceeded scope=%s key=%s total_usd=%.4f budget_usd=%.4f",
        scope,
        scope_key,
        total_usd,
        budget_usd,
    )
    return AlertDecision(
        scope=scope,
        scope_key=scope_key,
        total_usd=total_usd,
        budget_usd=budget_usd,
        exceeded=True,
        alert_emitted=True,
        cooldown_active=False,
        event_id=event_id,
    )


def check_application_budget(
    db: Session,
    application_id: str,
    *,
    publisher: Optional[EventPublisher] = None,
    budget_usd: Optional[float] = None,
) -> AlertDecision:
    """Compare ``application_id`` total against the per-application budget.

    Emits ``cost_budget_exceeded`` (scope=``application``) when the
    rolled-up total exceeds ``MAX_COST_PER_APPLICATION_USD`` AND the
    24h cooldown for this application has elapsed.
    """
    if not application_id:
        raise ValueError("application_id_required")
    total = sum_application_total_usd(db, application_id)
    budget = float(budget_usd) if budget_usd is not None else _max_per_application()
    return _evaluate(
        db,
        scope=SCOPE_APPLICATION,
        scope_key=str(application_id),
        total_usd=total,
        budget_usd=budget,
        publisher=publisher,
    )


def check_daily_budget(
    db: Session,
    *,
    day: Optional[datetime] = None,
    publisher: Optional[EventPublisher] = None,
    budget_usd: Optional[float] = None,
) -> AlertDecision:
    """Compare today's UTC total against ``MAX_COST_PER_DAY_USD``.

    Same cooldown semantics as :func:`check_application_budget`. The
    scope_key is the UTC date (``YYYY-MM-DD``) so the cooldown wraps
    naturally at midnight.
    """
    when = (day or _utc_now()).astimezone(timezone.utc)
    total = sum_daily_total_usd(db, day=when)
    budget = float(budget_usd) if budget_usd is not None else _max_per_day()
    return _evaluate(
        db,
        scope=SCOPE_DAILY,
        scope_key=when.strftime("%Y-%m-%d"),
        total_usd=total,
        budget_usd=budget,
        publisher=publisher,
    )
