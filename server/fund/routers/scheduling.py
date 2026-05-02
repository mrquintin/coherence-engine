"""Partner-meeting scheduling routes (prompt 54).

Two endpoints:

* ``POST /scheduling/proposals`` -- mint a :class:`MeetingProposal`
  by querying the configured backend for partner availability.
  Operator/analyst gated -- the founder portal does not call this
  directly; it is driven by ``ApplicationService`` after a ``pass``
  decision and exposed here for manual re-runs and tests.
* ``POST /scheduling/book`` -- founder click-through endpoint that
  consumes a proposal token + chosen slot, books the calendar
  event, and persists a :class:`Booking`. Idempotent on a repeated
  ``token`` (returns the existing booking unchanged). Returns HTTP
  410 GONE on an expired token.

Backend selection is delegated to a lazy factory so tests can
inject an :class:`InMemorySchedulerBackend` without monkey-patching
the env-driven resolver.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund.api_utils import (
    envelope,
    error_response,
    new_request_id,
)
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.security import enforce_roles
from coherence_engine.server.fund.services.scheduler import (
    ProposalCancelledError,
    ProposalNotFoundError,
    Scheduler,
    SchedulerError,
    TokenExpiredError,
)
from coherence_engine.server.fund.services.scheduler_backends import (
    SchedulerBackend,
    SchedulerBackendConfigError,
    SchedulerBackendError,
    scheduler_backend_from_env,
)


router = APIRouter(prefix="/scheduling", tags=["scheduling"])
LOGGER = logging.getLogger("coherence_engine.fund.scheduling")


# ---------------------------------------------------------------------------
# Backend factory injection point (test seam)
# ---------------------------------------------------------------------------


_BACKEND_FACTORY = scheduler_backend_from_env
_SCHEDULER_FACTORY: Optional[Any] = None


def set_scheduler_backend_factory_for_tests(factory) -> None:
    """Override the backend factory used by the routes (test-only)."""
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = factory


def reset_scheduler_backend_factory_for_tests() -> None:
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = scheduler_backend_from_env


def set_scheduler_factory_for_tests(factory) -> None:
    """Override the full :class:`Scheduler` factory (test-only)."""
    global _SCHEDULER_FACTORY
    _SCHEDULER_FACTORY = factory


def reset_scheduler_factory_for_tests() -> None:
    global _SCHEDULER_FACTORY
    _SCHEDULER_FACTORY = None


def _resolve_scheduler() -> Scheduler:
    if _SCHEDULER_FACTORY is not None:
        return _SCHEDULER_FACTORY()
    backend: SchedulerBackend = _BACKEND_FACTORY()
    return Scheduler(backend=backend)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/proposals", status_code=201)
def create_proposal_route(
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    denied = enforce_roles(request, ("analyst", "admin"))
    if denied:
        return denied

    application_id = str(body.get("application_id") or "").strip()
    partner_email = str(body.get("partner_email") or "").strip()
    founder_email = str(body.get("founder_email") or "").strip()
    duration_min = int(body.get("duration_min") or 30)
    if not application_id:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "application_id is required"
        )
    if not partner_email:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "partner_email is required"
        )

    try:
        scheduler = _resolve_scheduler()
    except SchedulerBackendConfigError as exc:
        return error_response(
            request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
        )

    try:
        result = scheduler.propose(
            db,
            application_id=application_id,
            partner_email=partner_email,
            founder_email=founder_email,
            duration_min=duration_min,
        )
    except SchedulerBackendError as exc:
        return error_response(request_id, 502, "PROVIDER_ERROR", str(exc))
    except SchedulerError as exc:
        return error_response(request_id, 400, "VALIDATION_ERROR", str(exc))

    db.commit()
    return envelope(
        request_id=request_id,
        data={
            "proposal_id": result.proposal.id,
            "application_id": result.proposal.application_id,
            "token": result.proposal.token,
            "booking_url": result.booking_url,
            "status": result.proposal.status,
            "expires_at": result.proposal.expires_at.isoformat(),
            "proposed_slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                }
                for s in result.slots
            ],
        },
    )


@router.post("/book")
def book_proposal_route(
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()

    token = str(body.get("token") or "").strip()
    chosen_raw = body.get("chosen_slot_start")
    if not token:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "token is required"
        )
    if not chosen_raw:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "chosen_slot_start is required"
        )

    try:
        chosen_slot_start = datetime.fromisoformat(
            str(chosen_raw).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return error_response(
            request_id,
            400,
            "VALIDATION_ERROR",
            "chosen_slot_start must be an ISO-8601 timestamp",
        )

    try:
        scheduler = _resolve_scheduler()
    except SchedulerBackendConfigError as exc:
        return error_response(
            request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
        )

    try:
        result = scheduler.book(
            db,
            token=token,
            chosen_slot_start=chosen_slot_start,
        )
    except ProposalNotFoundError:
        return error_response(
            request_id, 404, "NOT_FOUND", "no proposal matches the supplied token"
        )
    except TokenExpiredError:
        # Commit the lazy expiry transition (status -> "expired") that
        # ``Scheduler.book`` flushed before raising.
        db.commit()
        return error_response(
            request_id, 410, "GONE", "proposal token has expired"
        )
    except ProposalCancelledError:
        return error_response(
            request_id, 409, "CONFLICT", "proposal has been cancelled"
        )
    except SchedulerBackendError as exc:
        return error_response(request_id, 502, "PROVIDER_ERROR", str(exc))
    except SchedulerError as exc:
        return error_response(request_id, 400, "VALIDATION_ERROR", str(exc))

    db.commit()
    return envelope(
        request_id=request_id,
        data={
            "booking_id": result.booking.id,
            "proposal_id": result.proposal.id,
            "application_id": result.proposal.application_id,
            "provider_event_id": result.booking.provider_event_id,
            "scheduled_start": result.booking.scheduled_start.isoformat(),
            "scheduled_end": result.booking.scheduled_end.isoformat(),
            "backend": result.booking.backend,
            "status": result.booking.status,
            "idempotent_replay": result.idempotent_replay,
        },
    )
