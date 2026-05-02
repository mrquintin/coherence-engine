"""Per-application cost telemetry (prompt 62).

Every paid external call (STT minute, LLM token bundle, embeddings,
Twilio voice minute, Stripe fee) flows through :func:`record_cost`.
The function looks up ``unit_cost_usd`` in the governed YAML pricing
registry (:mod:`cost_pricing`), computes ``total_usd = units *
unit_cost_usd``, and writes a :class:`~CostEvent` row.

Idempotency
-----------

``record_cost`` requires an explicit ``idempotency_key``. The unique
index on ``CostEvent.idempotency_key`` makes a second write with the
same key a no-op: the existing row is returned. This makes the API
safe under webhook retries (Twilio recording-completion delivers more
than once on flaky networks, Stripe redelivers on receipt timeout).

Recording rules (prompt 62 prohibitions)
----------------------------------------

* The caller MUST compute ``units`` from the *observed* input/output
  -- never trust a number a client supplied. e.g. STT minutes come
  from the recording duration we already persisted; LLM tokens come
  from the SDK's response usage block.
* Pricing comes from ``data/governed/cost_pricing.yaml``, never from
  a code constant. An unknown SKU raises ``CostPricingError`` so the
  caller fails loud rather than silently writing ``$0``.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.cost_pricing import (
    PriceEntry,
    get_price,
)


__all__ = [
    "CostEvent",
    "CostTelemetryError",
    "RecordedCost",
    "compute_idempotency_key",
    "record_cost",
    "sum_application_total_usd",
    "sum_daily_total_usd",
]


_LOG = logging.getLogger("coherence_engine.fund.cost_telemetry")


# Re-exported so callers needing the ORM type can avoid an extra import.
CostEvent = models.CostEvent


class CostTelemetryError(RuntimeError):
    """Raised on invalid inputs to :func:`record_cost`."""


@dataclass(frozen=True)
class RecordedCost:
    """Return shape of :func:`record_cost` -- the persisted row + a `created`
    flag that distinguishes a fresh insert from an idempotent replay."""

    event: models.CostEvent
    created: bool


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id() -> str:
    return f"cst_{uuid.uuid4().hex[:16]}"


def compute_idempotency_key(
    *,
    provider: str,
    sku: str,
    application_id: Optional[str],
    discriminator: str,
) -> str:
    """Return a deterministic idempotency key for a paid call.

    ``discriminator`` is the upstream's natural id for the event --
    Twilio call SID, Deepgram request id, OpenAI response id, Stripe
    charge id, etc. Two callers with the same logical event (the same
    Twilio CallSid for the same application) produce the same key and
    therefore the same row.
    """
    parts = "|".join(
        [
            str(provider).strip(),
            str(sku).strip(),
            str(application_id or "").strip(),
            str(discriminator).strip(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _coerce_units(units: float) -> float:
    try:
        value = float(units)
    except (TypeError, ValueError) as exc:
        raise CostTelemetryError(f"cost_units_not_numeric:{units!r}") from exc
    if value < 0:
        raise CostTelemetryError(f"cost_units_negative:{value}")
    return value


def record_cost(
    db: Session,
    *,
    provider: str,
    sku: str,
    units: float,
    application_id: Optional[str],
    idempotency_key: str,
    occurred_at: Optional[datetime] = None,
    pricing_path: Optional[Any] = None,
) -> RecordedCost:
    """Persist a single paid-call ``CostEvent`` row.

    Args:
        db: Open SQLAlchemy session. The caller owns commit; this
            function flushes but does not commit.
        provider: Operator-readable provider name (``"deepgram"``,
            ``"openai"``, ``"twilio"``, ``"stripe"``). Mostly used
            for filtering in the partner-dashboard cost view.
        sku: The pricing-registry SKU (e.g.
            ``"deepgram.nova-2.audio_minute"``). Looked up in
            ``data/governed/cost_pricing.yaml`` -- an unknown SKU is
            a hard failure (``CostPricingError``).
        units: Server-observed units count (minutes, 1000-token
            buckets, requests). Negative values are rejected.
        application_id: The application this cost should be charged
            against. ``None`` is allowed for cross-cutting infra cost
            (background polling, baseline maintenance jobs).
        idempotency_key: Deterministic key (see
            :func:`compute_idempotency_key`). The unique index on
            ``CostEvent`` makes a second call with the same key
            return the existing row.
        occurred_at: When the upstream call actually occurred. Defaults
            to ``now`` -- callers with a webhook timestamp should pass
            it through so the daily roll-up is anchored to the real
            time-of-cost, not the time-of-recording.
        pricing_path: Override the pricing YAML path (tests).

    Returns:
        A :class:`RecordedCost` whose ``created`` flag is False when
        an existing row was returned via the idempotency check.

    Raises:
        CostTelemetryError: on negative units / blank inputs.
        CostPricingError: on an unknown SKU.
    """
    if not str(provider).strip():
        raise CostTelemetryError("cost_provider_required")
    if not str(sku).strip():
        raise CostTelemetryError("cost_sku_required")
    if not str(idempotency_key).strip():
        raise CostTelemetryError("cost_idempotency_key_required")
    units_value = _coerce_units(units)

    price: PriceEntry = get_price(sku, path=pricing_path)
    total_usd = round(units_value * price.unit_cost_usd, 6)
    when = occurred_at or _utc_now()

    existing = (
        db.query(models.CostEvent)
        .filter(models.CostEvent.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        return RecordedCost(event=existing, created=False)

    row = models.CostEvent(
        id=_new_id(),
        application_id=application_id or None,
        provider=str(provider).strip(),
        sku=str(sku).strip(),
        units=units_value,
        unit=price.unit,
        unit_cost_usd=float(price.unit_cost_usd),
        total_usd=total_usd,
        idempotency_key=str(idempotency_key).strip(),
        occurred_at=when,
        created_at=_utc_now(),
    )
    db.add(row)
    try:
        db.flush()
    except Exception:
        # Race against a concurrent writer that beat us to the unique
        # index. Roll the failed insert out of the session and retry
        # the lookup -- the winner's row is what we return.
        db.rollback()
        existing = (
            db.query(models.CostEvent)
            .filter(models.CostEvent.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is None:
            raise
        return RecordedCost(event=existing, created=False)

    _LOG.info(
        "cost_event_recorded provider=%s sku=%s units=%s total_usd=%.6f"
        " application_id=%s idem=%s",
        row.provider,
        row.sku,
        row.units,
        row.total_usd,
        row.application_id or "-",
        row.idempotency_key[:12],
    )
    return RecordedCost(event=row, created=True)


def sum_application_total_usd(
    db: Session,
    application_id: str,
) -> float:
    """Return the total recorded ``total_usd`` for one application."""
    rows = (
        db.query(models.CostEvent.total_usd)
        .filter(models.CostEvent.application_id == application_id)
        .all()
    )
    return float(sum(float(r[0] or 0.0) for r in rows))


def sum_daily_total_usd(
    db: Session,
    *,
    day: Optional[datetime] = None,
) -> float:
    """Return the total recorded ``total_usd`` for a UTC calendar day.

    ``day`` defaults to the current UTC day. We range-filter on
    ``occurred_at`` rather than ``created_at`` so a back-dated webhook
    is attributed to the day the cost was actually incurred.
    """
    when = (day or _utc_now()).astimezone(timezone.utc)
    start = datetime(when.year, when.month, when.day, tzinfo=timezone.utc)
    # Add 24h via timedelta to avoid month/year boundary surprises.
    from datetime import timedelta

    end = start + timedelta(days=1)
    rows = (
        db.query(models.CostEvent.total_usd)
        .filter(models.CostEvent.occurred_at >= start)
        .filter(models.CostEvent.occurred_at < end)
        .all()
    )
    return float(sum(float(r[0] or 0.0) for r in rows))
