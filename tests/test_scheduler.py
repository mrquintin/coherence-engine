"""Scheduler / partner-meeting booking tests (prompt 54).

Covers the propose / book lifecycle implemented in
:mod:`coherence_engine.server.fund.services.scheduler` plus the
``POST /scheduling/proposals`` and ``POST /scheduling/book`` routes
in :mod:`coherence_engine.server.fund.routers.scheduling`. The Cal.com
and Google Calendar SDKs are NOT exercised -- the tests inject a
deterministic :class:`InMemorySchedulerBackend` so no network call
is ever made.

Three behavioural invariants are verified explicitly (the prompt
54 acceptance criteria):

* Mocked backend availability of >3 candidates collapses to exactly
  three distinct slots in the proposal payload.
* Booking with an expired token returns HTTP 410 Gone.
* A second book call for the same proposal returns the existing
  booking idempotently and does not invoke the backend twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except BaseException as _exc:  # pragma: no cover - dependency missing
    pytest.skip(
        f"FastAPI unavailable in this interpreter: {_exc}",
        allow_module_level=True,
    )

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.routers.scheduling import (
    reset_scheduler_backend_factory_for_tests,
    reset_scheduler_factory_for_tests,
    router as scheduling_router,
    set_scheduler_factory_for_tests,
)
from coherence_engine.server.fund.services.scheduler import (
    DEFAULT_PROPOSAL_TTL_HOURS,
    MAX_PROPOSED_SLOTS,
    ProposalNotFoundError,
    Scheduler,
    SchedulerError,
    TokenExpiredError,
    emit_scheduling_event,
)
from coherence_engine.server.fund.services.scheduler_backends import (
    BookedEvent,
    CalComBackend,
    GoogleCalendarBackend,
    InMemorySchedulerBackend,
    SchedulerBackendConfigError,
    SchedulerBackendError,
    Slot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _persist_application(suffix: str = "1") -> models.Application:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id=f"fnd_sched_{suffix}",
            full_name=f"Founder {suffix}",
            email=f"founder{suffix}@example.com",
            company_name=f"Company {suffix}",
            country="US",
        )
        db.add(founder)
        db.flush()
        app = models.Application(
            id=f"app_sched_{suffix}",
            founder_id=founder.id,
            one_liner="A pitch.",
            requested_check_usd=250_000,
            use_of_funds_summary="hire two engineers",
            preferred_channel="browser",
            status="decided",
        )
        db.add(app)
        db.commit()
        db.refresh(app)
        return app
    finally:
        db.close()


def _slot(day_offset: int, hour: int = 14, minutes: int = 30) -> Slot:
    base = datetime(2026, 5, 1, hour, 0, tzinfo=timezone.utc) + timedelta(days=day_offset)
    return Slot(base, base + timedelta(minutes=minutes))


def _backend_with_five_slots() -> InMemorySchedulerBackend:
    return InMemorySchedulerBackend(
        slots=[_slot(d) for d in range(5)],
    )


# ---------------------------------------------------------------------------
# Backend / value-type unit tests
# ---------------------------------------------------------------------------


def test_slot_rejects_naive_datetimes():
    naive = datetime(2026, 5, 1, 14, 0)
    with pytest.raises(ValueError):
        Slot(naive, naive + timedelta(minutes=30))


def test_slot_rejects_inverted_window():
    start = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        Slot(start, start - timedelta(minutes=5))


def test_in_memory_backend_caps_at_three():
    backend = _backend_with_five_slots()
    slots = backend.availability(
        partner_email="alice@vc.example",
        duration_min=30,
        window_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc),
    )
    assert len(slots) == MAX_PROPOSED_SLOTS == 3
    assert all(isinstance(s, Slot) for s in slots)
    starts = [s.start for s in slots]
    assert starts == sorted(set(starts))


def test_in_memory_backend_book_returns_provider_event_id():
    backend = _backend_with_five_slots()
    slot = _slot(1)
    event = backend.book(
        slot=slot,
        partner_email="alice@vc.example",
        founder_email="founder@startup.example",
        application_id="app_x",
    )
    assert isinstance(event, BookedEvent)
    assert event.provider_event_id.startswith("in-mem-")
    assert event.start == slot.start


def test_in_memory_backend_double_book_raises():
    backend = _backend_with_five_slots()
    slot = _slot(1)
    backend.book(
        slot=slot,
        partner_email="a@v.example",
        founder_email="f@s.example",
        application_id="app_x",
    )
    with pytest.raises(SchedulerBackendError):
        backend.book(
            slot=slot,
            partner_email="a@v.example",
            founder_email="f@s.example",
            application_id="app_x",
        )


def test_calcom_backend_requires_api_key():
    with pytest.raises(SchedulerBackendConfigError):
        CalComBackend(api_key="")


def test_google_calendar_backend_requires_oauth():
    with pytest.raises(SchedulerBackendConfigError):
        GoogleCalendarBackend(
            client_id="",
            client_secret="",
            refresh_token="",
        )


# ---------------------------------------------------------------------------
# Scheduler.propose
# ---------------------------------------------------------------------------


def test_propose_writes_three_distinct_slots():
    application = _persist_application("a")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        result = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
            founder_email="founder@startup.example",
            duration_min=30,
        )
        db.commit()

        assert result.proposal.status == "pending"
        assert result.proposal.token
        assert result.booking_url.endswith(f"?token={result.proposal.token}")
        assert len(result.slots) == 3
        starts = [s.start for s in result.slots]
        assert len(set(starts)) == 3, "proposed slots must be distinct"

        persisted = (
            db.query(models.MeetingProposal)
            .filter(models.MeetingProposal.id == result.proposal.id)
            .one()
        )
        assert persisted.proposed_slots_json
        assert persisted.backend == "in_memory"
        assert persisted.partner_email == "alice@vc.example"
        # TTL is 72h by default and expires_at is created_at + ttl.
        assert (
            persisted.expires_at - persisted.created_at
        ).total_seconds() == DEFAULT_PROPOSAL_TTL_HOURS * 3600
    finally:
        db.close()


def test_propose_requires_application_and_partner():
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        with pytest.raises(SchedulerError):
            scheduler.propose(db, application_id="", partner_email="x@y")
        with pytest.raises(SchedulerError):
            scheduler.propose(
                db, application_id="app", partner_email=""
            )
    finally:
        db.close()


def test_propose_with_no_availability_marks_expired():
    application = _persist_application("nomatch")
    backend = InMemorySchedulerBackend(slots=[])
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        result = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
        )
        db.commit()
        assert result.slots == []
        assert result.proposal.status == "expired"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scheduler.book
# ---------------------------------------------------------------------------


def test_book_writes_booking_and_marks_proposal_booked():
    application = _persist_application("b")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        proposed = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
            founder_email="founder@startup.example",
        )
        db.commit()
        chosen = proposed.slots[0]

        booked = scheduler.book(
            db,
            token=proposed.proposal.token,
            chosen_slot_start=chosen.start,
        )
        db.commit()

        assert booked.idempotent_replay is False
        assert booked.booking.proposal_id == proposed.proposal.id
        assert booked.booking.scheduled_start == chosen.start
        assert booked.booking.provider_event_id.startswith("in-mem-")
        assert booked.proposal.status == "booked"
        assert booked.proposal.booked_at is not None
    finally:
        db.close()


def test_book_idempotent_on_repeated_token():
    application = _persist_application("idem")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        proposed = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
            founder_email="founder@startup.example",
        )
        db.commit()
        chosen_start = proposed.slots[0].start

        first = scheduler.book(
            db,
            token=proposed.proposal.token,
            chosen_slot_start=chosen_start,
        )
        db.commit()
        second = scheduler.book(
            db,
            token=proposed.proposal.token,
            chosen_slot_start=chosen_start,
        )
        db.commit()

        assert first.booking.id == second.booking.id
        assert second.idempotent_replay is True
        assert (
            db.query(models.Booking)
            .filter(models.Booking.proposal_id == proposed.proposal.id)
            .count()
        ) == 1
    finally:
        db.close()


def test_book_raises_token_expired_after_ttl():
    application = _persist_application("exp")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(
        backend=backend,
        proposal_ttl=timedelta(seconds=1),
    )
    db = SessionLocal()
    try:
        # Use a fixed "now" for the propose call so we can pass a
        # later "now" to the book call deterministically. The
        # availability window is opened explicitly to cover the
        # synthetic slot range used by ``_backend_with_five_slots``.
        anchor = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
        proposed = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
            founder_email="founder@startup.example",
            now=anchor,
            window_start=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 31, 0, 0, tzinfo=timezone.utc),
        )
        db.commit()
        assert proposed.slots, "expected non-empty proposed slots"
        chosen_start = proposed.slots[0].start

        with pytest.raises(TokenExpiredError):
            scheduler.book(
                db,
                token=proposed.proposal.token,
                chosen_slot_start=chosen_start,
                now=anchor + timedelta(hours=1),
            )

        # The lazy expiry transition should have flipped status.
        db.expire_all()
        refreshed = (
            db.query(models.MeetingProposal)
            .filter(models.MeetingProposal.id == proposed.proposal.id)
            .one()
        )
        assert refreshed.status == "expired"
    finally:
        db.close()


def test_book_unknown_token_raises_not_found():
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        with pytest.raises(ProposalNotFoundError):
            scheduler.book(
                db,
                token="ffffffffffffffffffffffffffffffff",
                chosen_slot_start=datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc),
            )
    finally:
        db.close()


def test_book_chosen_slot_not_in_proposal_raises():
    application = _persist_application("badslot")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    db = SessionLocal()
    try:
        proposed = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
        )
        db.commit()
        with pytest.raises(SchedulerError):
            scheduler.book(
                db,
                token=proposed.proposal.token,
                chosen_slot_start=datetime(
                    2099, 1, 1, 0, 0, tzinfo=timezone.utc
                ),
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


class _FakeEvents:
    def __init__(self):
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"event_id": f"evt_{len(self.published)}"}


def test_book_emits_meeting_booked_event():
    application = _persist_application("evt")
    backend = _backend_with_five_slots()
    fake_events = _FakeEvents()
    scheduler = Scheduler(backend=backend, events=fake_events)
    db = SessionLocal()
    try:
        proposed = scheduler.propose(
            db,
            application_id=application.id,
            partner_email="alice@vc.example",
            founder_email="founder@startup.example",
        )
        db.commit()
        scheduler.book(
            db,
            token=proposed.proposal.token,
            chosen_slot_start=proposed.slots[0].start,
        )
        db.commit()
    finally:
        db.close()

    booked_events = [
        e for e in fake_events.published if e["event_type"] == "meeting_booked"
    ]
    assert len(booked_events) == 1
    payload = booked_events[0]["payload"]
    assert payload["application_id"] == application.id
    assert payload["partner_email"] == "alice@vc.example"
    assert payload["founder_email"] == "founder@startup.example"
    assert payload["scheduled_start"].endswith("Z")


def test_emit_scheduling_event_publishes():
    fake_events = _FakeEvents()
    emit_scheduling_event(
        fake_events,
        application_id="app_q",
        partner_email="alice@vc.example",
        trace_id="trc_z",
        idempotency_key="app_q:SchedulingRequested",
    )
    assert any(
        e["event_type"] == "scheduling_requested" for e in fake_events.published
    )


def test_emit_scheduling_event_handles_none_publisher():
    # Must not raise even when no event publisher is available.
    emit_scheduling_event(
        None,
        application_id="app_q",
        partner_email="x@y",
        trace_id="trc_q",
        idempotency_key="k",
    )


# ---------------------------------------------------------------------------
# Router tests (FastAPI TestClient)
# ---------------------------------------------------------------------------


def _make_app(scheduler: Scheduler, principal: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(scheduling_router, prefix="/api/v1")
    app.include_router(scheduling_router)

    class _PrincipalStamper(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if principal is not None:
                request.state.principal = dict(principal)
            return await call_next(request)

    app.add_middleware(_PrincipalStamper)
    set_scheduler_factory_for_tests(lambda: scheduler)
    return app


@pytest.fixture
def analyst_principal() -> dict:
    return {
        "auth_type": "test",
        "role": "analyst",
        "fingerprint": "test-analyst",
        "key_id": "key_test",
    }


@pytest.fixture(autouse=True)
def _reset_router_factories():
    yield
    reset_scheduler_factory_for_tests()
    reset_scheduler_backend_factory_for_tests()


def test_route_propose_returns_three_slots(analyst_principal):
    application = _persist_application("rpr")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    client = TestClient(_make_app(scheduler, analyst_principal))

    res = client.post(
        "/api/v1/scheduling/proposals",
        json={
            "application_id": application.id,
            "partner_email": "alice@vc.example",
            "founder_email": "founder@startup.example",
            "duration_min": 30,
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()["data"]
    assert len(data["proposed_slots"]) == 3
    assert data["status"] == "pending"
    assert data["token"]
    assert data["booking_url"].endswith(f"?token={data['token']}")


def test_route_propose_requires_role(analyst_principal):
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    # No principal -> enforce_roles returns 403.
    client = TestClient(_make_app(scheduler, principal=None))
    res = client.post(
        "/api/v1/scheduling/proposals",
        json={"application_id": "x", "partner_email": "y@z"},
    )
    assert res.status_code == 403


def test_route_book_returns_410_on_expired_token(analyst_principal):
    application = _persist_application("rxp")
    backend = _backend_with_five_slots()

    # Mint with normal TTL, then surface an expired-state proposal
    # by handing back a scheduler whose proposal_ttl is < 0.
    scheduler = Scheduler(
        backend=backend,
        proposal_ttl=timedelta(seconds=-1),
    )
    client = TestClient(_make_app(scheduler, analyst_principal))

    propose_res = client.post(
        "/api/v1/scheduling/proposals",
        json={
            "application_id": application.id,
            "partner_email": "alice@vc.example",
            "founder_email": "founder@startup.example",
        },
    )
    assert propose_res.status_code == 201
    payload = propose_res.json()["data"]
    chosen = payload["proposed_slots"][0]["start"]

    book_res = client.post(
        "/api/v1/scheduling/book",
        json={"token": payload["token"], "chosen_slot_start": chosen},
    )
    assert book_res.status_code == 410, book_res.text
    err = book_res.json()["error"]
    assert err["code"] == "GONE"


def test_route_book_idempotent(analyst_principal):
    application = _persist_application("rid")
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    client = TestClient(_make_app(scheduler, analyst_principal))

    propose_res = client.post(
        "/api/v1/scheduling/proposals",
        json={
            "application_id": application.id,
            "partner_email": "alice@vc.example",
            "founder_email": "founder@startup.example",
        },
    )
    payload = propose_res.json()["data"]
    chosen = payload["proposed_slots"][0]["start"]

    first = client.post(
        "/api/v1/scheduling/book",
        json={"token": payload["token"], "chosen_slot_start": chosen},
    )
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert first_data["idempotent_replay"] is False

    second = client.post(
        "/api/v1/scheduling/book",
        json={"token": payload["token"], "chosen_slot_start": chosen},
    )
    assert second.status_code == 200, second.text
    second_data = second.json()["data"]
    assert second_data["booking_id"] == first_data["booking_id"]
    assert second_data["idempotent_replay"] is True

    db = SessionLocal()
    try:
        assert (
            db.query(models.Booking)
            .filter(models.Booking.application_id == application.id)
            .count()
        ) == 1
    finally:
        db.close()


def test_route_book_unknown_token_returns_404(analyst_principal):
    backend = _backend_with_five_slots()
    scheduler = Scheduler(backend=backend)
    client = TestClient(_make_app(scheduler, analyst_principal))
    res = client.post(
        "/api/v1/scheduling/book",
        json={
            "token": "deadbeef" * 4,
            "chosen_slot_start": "2026-05-01T14:00:00+00:00",
        },
    )
    assert res.status_code == 404


def test_route_propose_provider_unavailable():
    backend = _backend_with_five_slots()
    _scheduler = Scheduler(backend=backend)

    def _factory():
        raise SchedulerBackendConfigError("calcom_api_key_missing")

    set_scheduler_factory_for_tests(_factory)

    app = FastAPI()
    app.include_router(scheduling_router, prefix="/api/v1")

    class _PrincipalStamper(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.principal = {
                "auth_type": "test",
                "role": "analyst",
                "fingerprint": "x",
                "key_id": "y",
            }
            return await call_next(request)

    app.add_middleware(_PrincipalStamper)
    client = TestClient(app)
    res = client.post(
        "/api/v1/scheduling/proposals",
        json={"application_id": "app", "partner_email": "x@y"},
    )
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
