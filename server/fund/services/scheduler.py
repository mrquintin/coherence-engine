"""Partner-meeting scheduling service (prompt 54).

Bridges :class:`~coherence_engine.server.fund.services.scheduler_backends.SchedulerBackend`
implementations (Cal.com primary, Google Calendar fallback) with the
:class:`MeetingProposal` / :class:`Booking` ledger.

Flow
----

1. ``ApplicationService`` issues an enforce-mode ``pass`` decision and
   calls :func:`emit_scheduling_event`. The event lands in the outbox
   and a downstream worker (or the founder portal click) drives
   :func:`propose`.
2. :func:`propose` queries the backend for availability inside a
   bounded time window, picks the top three slots (hard cap: prompt
   54 prohibition), persists a :class:`MeetingProposal` row with a
   short-lived token, and returns the founder-facing URL.
3. The founder click-through hits ``POST /scheduling/book`` with
   ``token`` + chosen slot. :func:`book` validates the token has not
   expired (HTTP 410 on expired), calls
   :meth:`SchedulerBackend.book` to write the calendar event, and
   persists a :class:`Booking` row. A second call with the same
   ``token`` returns the existing :class:`Booking` row idempotently.
4. A ``meeting_booked`` event is emitted via the existing
   :class:`EventPublisher` outbox so downstream systems
   (notifications, analytics) can react.

Token model
-----------

Tokens are 32 random hex chars (16 bytes from ``secrets.token_hex``)
with a default 72-hour expiry. They are stored in the
:attr:`MeetingProposal.token` column and uniqueness-enforced by the
table's index. Expiry is checked in code -- there is NO env-gated
bypass (prompt 54 prohibition: do NOT bypass token expiry checks).
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.scheduler_backends import (
    BookedEvent,
    SchedulerBackend,
    SchedulerBackendError,
    Slot,
)


__all__ = [
    "Scheduler",
    "SchedulerError",
    "TokenExpiredError",
    "ProposalNotFoundError",
    "ProposalCancelledError",
    "ProposeResult",
    "BookResult",
    "DEFAULT_PROPOSAL_TTL_HOURS",
    "DEFAULT_AVAILABILITY_WINDOW_DAYS",
    "MAX_PROPOSED_SLOTS",
    "emit_scheduling_event",
]


_LOG = logging.getLogger(__name__)


DEFAULT_PROPOSAL_TTL_HOURS = 72
DEFAULT_AVAILABILITY_WINDOW_DAYS = 14
MAX_PROPOSED_SLOTS = 3


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchedulerError(Exception):
    """Base class for scheduler-service-level errors."""


class ProposalNotFoundError(SchedulerError):
    """Raised by :meth:`Scheduler.book` when the token does not match a row."""


class TokenExpiredError(SchedulerError):
    """Raised by :meth:`Scheduler.book` when the proposal is past ``expires_at``."""


class ProposalCancelledError(SchedulerError):
    """Raised when a proposal has been operator-cancelled before booking."""


# ---------------------------------------------------------------------------
# Result envelopes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposeResult:
    proposal: models.MeetingProposal
    booking_url: str
    slots: List[Slot]


@dataclass(frozen=True)
class BookResult:
    proposal: models.MeetingProposal
    booking: models.Booking
    booked_event: BookedEvent
    idempotent_replay: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_token() -> str:
    return secrets.token_hex(16)


def _new_proposal_id() -> str:
    return f"mtgp_{uuid.uuid4().hex[:16]}"


def _new_booking_id() -> str:
    return f"bkng_{uuid.uuid4().hex[:16]}"


def _serialize_slots(slots: List[Slot]) -> str:
    return json.dumps(
        [
            {
                "start": _ensure_utc(s.start).isoformat().replace("+00:00", "Z"),
                "end": _ensure_utc(s.end).isoformat().replace("+00:00", "Z"),
            }
            for s in slots
        ]
    )


def _deserialize_slots(raw: str) -> List[Slot]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out: List[Slot] = []
    for item in items:
        try:
            start = _ensure_utc(
                datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            )
            end = _ensure_utc(
                datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00"))
            )
            out.append(Slot(start, end))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _match_chosen_slot(slots: List[Slot], chosen_start: datetime) -> Optional[Slot]:
    target = _ensure_utc(chosen_start)
    for s in slots:
        if s.start == target:
            return s
    return None


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def emit_scheduling_event(
    events: Any,
    *,
    application_id: str,
    partner_email: str,
    trace_id: str,
    idempotency_key: str,
) -> None:
    """Publish a ``scheduling_requested`` event to the outbox.

    Called by :class:`ApplicationService` after an enforce-mode
    ``pass`` decision is written. The downstream worker (or the
    founder portal) calls :meth:`Scheduler.propose` in response.
    Failures are swallowed by the caller -- the ``DecisionIssued``
    event and the decision row remain authoritative.
    """
    if events is None:
        return
    try:
        events.publish(
            event_type="scheduling_requested",
            producer="scheduler",
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            payload={
                "application_id": application_id,
                "partner_email": partner_email,
            },
        )
    except Exception:  # pragma: no cover - best-effort
        _LOG.exception(
            "scheduling_requested_publish_failed application_id=%s",
            application_id,
        )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Orchestrates :class:`MeetingProposal` and :class:`Booking` rows.

    Holds a reference to a :class:`SchedulerBackend` (Cal.com or
    Google Calendar) plus an optional :class:`EventPublisher` so
    successful bookings emit a ``meeting_booked`` outbox event.

    Attributes
    ----------
    backend:
        The active :class:`SchedulerBackend` implementation.
    events:
        Optional :class:`EventPublisher`. When provided,
        :meth:`book` emits a ``meeting_booked`` event after a
        successful booking.
    booking_url_base:
        The founder-facing URL that the click-through targets. The
        scheduler appends ``?token=<token>`` to this base.
    proposal_ttl:
        How long a proposal token is valid before it is rejected.
    """

    def __init__(
        self,
        *,
        backend: SchedulerBackend,
        events: Optional[Any] = None,
        booking_url_base: str = "https://app.example.com/scheduling/book",
        proposal_ttl: timedelta = timedelta(hours=DEFAULT_PROPOSAL_TTL_HOURS),
    ) -> None:
        self.backend = backend
        self.events = events
        self.booking_url_base = booking_url_base.rstrip("?&")
        self.proposal_ttl = proposal_ttl

    # ------------------------------------------------------------------
    # Propose
    # ------------------------------------------------------------------

    def propose(
        self,
        session: Session,
        *,
        application_id: str,
        partner_email: str,
        founder_email: str = "",
        duration_min: int = 30,
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> ProposeResult:
        """Query backend availability, persist a proposal, return click-through URL.

        At most :data:`MAX_PROPOSED_SLOTS` slots are returned even
        when the backend offers more (prompt 54 prohibition: do NOT
        propose more than three slots).
        """
        if not application_id:
            raise SchedulerError("application_id_required")
        if not partner_email:
            raise SchedulerError("partner_email_required")
        if duration_min <= 0:
            raise SchedulerError("duration_min_positive_required")

        now = now or _utc_now()
        window_start = _ensure_utc(window_start or (now + timedelta(hours=1)))
        window_end = _ensure_utc(
            window_end or (now + timedelta(days=DEFAULT_AVAILABILITY_WINDOW_DAYS))
        )

        try:
            offered = self.backend.availability(
                partner_email=partner_email,
                duration_min=duration_min,
                window_start=window_start,
                window_end=window_end,
            )
        except SchedulerBackendError:
            raise
        if len(offered) > MAX_PROPOSED_SLOTS:
            offered = offered[:MAX_PROPOSED_SLOTS]

        token = _new_token()
        proposal = models.MeetingProposal(
            id=_new_proposal_id(),
            application_id=application_id,
            partner_email=partner_email,
            founder_email=founder_email,
            duration_min=duration_min,
            proposed_slots_json=_serialize_slots(offered),
            token=token,
            status="pending" if offered else "expired",
            backend=getattr(self.backend, "name", "") or "",
            created_at=now,
            expires_at=now + self.proposal_ttl,
        )
        session.add(proposal)
        session.flush()

        url = f"{self.booking_url_base}?token={token}"
        return ProposeResult(proposal=proposal, booking_url=url, slots=offered)

    # ------------------------------------------------------------------
    # Book
    # ------------------------------------------------------------------

    def book(
        self,
        session: Session,
        *,
        token: str,
        chosen_slot_start: datetime,
        now: Optional[datetime] = None,
    ) -> BookResult:
        """Book ``chosen_slot_start`` against the proposal addressed by ``token``.

        Idempotent: a second call with the same ``token`` after a
        successful booking returns the existing :class:`Booking` row
        unchanged (no second backend call). An expired token raises
        :class:`TokenExpiredError`. A cancelled proposal raises
        :class:`ProposalCancelledError`. An unknown token raises
        :class:`ProposalNotFoundError`.
        """
        now = now or _utc_now()

        proposal = (
            session.query(models.MeetingProposal)
            .filter(models.MeetingProposal.token == token)
            .one_or_none()
        )
        if proposal is None:
            raise ProposalNotFoundError(f"unknown_token:{token!r}")

        if proposal.status == "booked":
            existing = (
                session.query(models.Booking)
                .filter(models.Booking.proposal_id == proposal.id)
                .one_or_none()
            )
            if existing is None:
                # Defensive: status says booked but no booking row -- treat
                # as non-replay so the caller observes the inconsistency.
                raise SchedulerError(
                    f"proposal_booked_without_booking:{proposal.id}"
                )
            return BookResult(
                proposal=proposal,
                booking=existing,
                booked_event=BookedEvent(
                    provider_event_id=existing.provider_event_id,
                    start=existing.scheduled_start,
                    end=existing.scheduled_end,
                ),
                idempotent_replay=True,
            )

        if proposal.status == "cancelled":
            raise ProposalCancelledError(f"proposal_cancelled:{proposal.id}")

        if _ensure_utc(proposal.expires_at) <= now:
            if proposal.status == "pending":
                proposal.status = "expired"
                session.flush()
            raise TokenExpiredError(f"token_expired:{token!r}")

        slots = _deserialize_slots(proposal.proposed_slots_json)
        chosen = _match_chosen_slot(slots, chosen_slot_start)
        if chosen is None:
            raise SchedulerError(
                f"chosen_slot_not_offered:{_ensure_utc(chosen_slot_start).isoformat()}"
            )

        try:
            booked_event = self.backend.book(
                slot=chosen,
                partner_email=proposal.partner_email,
                founder_email=proposal.founder_email,
                application_id=proposal.application_id,
            )
        except SchedulerBackendError:
            raise

        booking = models.Booking(
            id=_new_booking_id(),
            proposal_id=proposal.id,
            application_id=proposal.application_id,
            backend=proposal.backend or getattr(self.backend, "name", "") or "",
            provider_event_id=booked_event.provider_event_id,
            partner_email=proposal.partner_email,
            founder_email=proposal.founder_email,
            scheduled_start=_ensure_utc(booked_event.start),
            scheduled_end=_ensure_utc(booked_event.end),
            status="confirmed",
            created_at=now,
        )
        session.add(booking)
        proposal.status = "booked"
        proposal.booked_at = now
        session.flush()

        self._emit_meeting_booked(
            proposal=proposal,
            booking=booking,
        )
        return BookResult(
            proposal=proposal,
            booking=booking,
            booked_event=booked_event,
            idempotent_replay=False,
        )

    # ------------------------------------------------------------------
    # Internal: meeting_booked outbox event
    # ------------------------------------------------------------------

    def _emit_meeting_booked(
        self,
        *,
        proposal: models.MeetingProposal,
        booking: models.Booking,
    ) -> None:
        if self.events is None:
            return
        payload: Dict[str, object] = {
            "application_id": proposal.application_id,
            "proposal_id": proposal.id,
            "booking_id": booking.id,
            "partner_email": proposal.partner_email,
            "founder_email": proposal.founder_email,
            "scheduled_start": _ensure_utc(booking.scheduled_start)
            .isoformat()
            .replace("+00:00", "Z"),
            "scheduled_end": _ensure_utc(booking.scheduled_end)
            .isoformat()
            .replace("+00:00", "Z"),
            "backend": booking.backend,
        }
        try:
            self.events.publish(
                event_type="meeting_booked",
                producer="scheduler",
                trace_id=f"trc_{proposal.id}",
                idempotency_key=f"meeting_booked:{booking.id}",
                payload=payload,
            )
        except Exception:  # pragma: no cover - best-effort
            _LOG.exception(
                "meeting_booked_publish_failed booking_id=%s", booking.id
            )
