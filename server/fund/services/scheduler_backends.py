"""Calendar/scheduling backends (prompt 54).

Two pluggable backends sit behind the :class:`SchedulerBackend`
protocol consumed by :class:`~coherence_engine.server.fund.services.scheduler.Scheduler`:

* :class:`CalComBackend` -- primary. Cal.com hosted scheduling API,
  used for partners with managed cal.com accounts.
* :class:`GoogleCalendarBackend` -- fallback for partners whose
  availability lives in a personal Google Calendar (Workspace
  domains supported via the same OAuth flow).

A test-only :class:`InMemorySchedulerBackend` is also exposed so
``tests/test_scheduler.py`` can exercise the full propose/book path
without touching the network.

The Cal.com and Google Calendar SDKs are imported lazily inside the
methods that need them (prompt 54 prohibition: do NOT make either
provider a hard dependency). When the SDK or env config is missing,
the backend constructor raises :class:`SchedulerBackendConfigError`
and the router surface returns 503 PROVIDER_UNAVAILABLE.

Storage discipline (prompt 54): no raw provider payloads are
persisted by these backends; the only state that crosses back to
the database is the proposed-slot list, the partner / founder
email addresses, and the opaque provider event id returned by
``book``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Protocol, Sequence


_LOG = logging.getLogger(__name__)


__all__ = [
    "Slot",
    "BookedEvent",
    "SchedulerBackend",
    "SchedulerBackendError",
    "SchedulerBackendConfigError",
    "CalComBackend",
    "GoogleCalendarBackend",
    "InMemorySchedulerBackend",
    "scheduler_backend_from_env",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchedulerBackendError(Exception):
    """Raised by a backend on a provider-side error (network, API)."""


class SchedulerBackendConfigError(SchedulerBackendError):
    """Raised when a backend cannot be constructed -- missing SDK, no API key, etc."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """A proposed availability window in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Slot.start/.end must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("Slot.end must be strictly after Slot.start")


@dataclass(frozen=True)
class BookedEvent:
    """The provider's confirmation of a booked slot."""

    provider_event_id: str
    start: datetime
    end: datetime


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class SchedulerBackend(Protocol):
    """Adapter surface every scheduling backend must implement.

    Implementations are responsible for translating the provider's
    native availability format into UTC :class:`Slot` instances and
    for returning a stable opaque ``provider_event_id`` from
    :meth:`book` so subsequent reschedule / cancel calls can target
    the same calendar event.
    """

    name: str

    def availability(
        self,
        *,
        partner_email: str,
        duration_min: int,
        window_start: datetime,
        window_end: datetime,
    ) -> List[Slot]:
        ...  # pragma: no cover - protocol stub

    def book(
        self,
        *,
        slot: Slot,
        partner_email: str,
        founder_email: str,
        application_id: str,
    ) -> BookedEvent:
        ...  # pragma: no cover - protocol stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pick_top_three(
    slots: Sequence[Slot],
    *,
    duration_min: int,
) -> List[Slot]:
    """Return at most three distinct slots, earliest first.

    "Distinct" means non-identical ``start`` -- two slots that begin
    at the same instant collapse to one. Bandwidth is finite (prompt
    54 prohibition: do NOT propose more than three slots) so the
    output is hard-capped at three even when the backend offers
    more.
    """
    seen_starts: set[datetime] = set()
    deduped: List[Slot] = []
    for s in sorted(slots, key=lambda x: x.start):
        if s.start in seen_starts:
            continue
        if (s.end - s.start) < timedelta(minutes=duration_min):
            continue
        seen_starts.add(s.start)
        deduped.append(s)
        if len(deduped) == 3:
            break
    return deduped


# ---------------------------------------------------------------------------
# Cal.com backend
# ---------------------------------------------------------------------------


class CalComBackend:
    """Primary backend -- Cal.com hosted scheduling API.

    The Cal.com SDK / HTTP client is imported lazily inside
    :meth:`availability` and :meth:`book` so the import cost is paid
    only when the backend is actually used. Tests exercise the
    in-memory backend instead.
    """

    name = "calcom"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = "https://api.cal.com/v1",
        event_type_id: Optional[str] = None,
    ) -> None:
        if not api_key:
            raise SchedulerBackendConfigError("calcom_api_key_missing")
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._event_type_id = event_type_id or ""

    def _client(self):
        # Lazy import: keep ``requests`` (or any specific SDK) out of
        # the import path for callers that never touch this backend.
        try:
            import requests  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised via Config
            raise SchedulerBackendConfigError(
                f"calcom_sdk_unavailable:{exc}"
            ) from exc
        return requests

    def availability(
        self,
        *,
        partner_email: str,
        duration_min: int,
        window_start: datetime,
        window_end: datetime,
    ) -> List[Slot]:
        client = self._client()
        params = {
            "apiKey": self._api_key,
            "userEmail": partner_email,
            "dateFrom": _ensure_utc(window_start).isoformat(),
            "dateTo": _ensure_utc(window_end).isoformat(),
            "duration": duration_min,
        }
        try:
            resp = client.get(f"{self._api_base}/availability", params=params, timeout=10)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # pragma: no cover - network path
            raise SchedulerBackendError(f"calcom_availability_failed:{exc}") from exc

        slots: List[Slot] = []
        for entry in payload.get("slots", []) or []:
            try:
                start = datetime.fromisoformat(str(entry["start"]).replace("Z", "+00:00"))
                end_raw = entry.get("end")
                if end_raw:
                    end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                else:
                    end = start + timedelta(minutes=duration_min)
                slots.append(Slot(_ensure_utc(start), _ensure_utc(end)))
            except (KeyError, ValueError):
                continue
        return _pick_top_three(slots, duration_min=duration_min)

    def book(
        self,
        *,
        slot: Slot,
        partner_email: str,
        founder_email: str,
        application_id: str,
    ) -> BookedEvent:
        client = self._client()
        body = {
            "eventTypeId": self._event_type_id,
            "start": _ensure_utc(slot.start).isoformat(),
            "end": _ensure_utc(slot.end).isoformat(),
            "responses": {
                "email": founder_email,
                "name": founder_email,
            },
            "metadata": {
                "application_id": application_id,
                "partner_email": partner_email,
            },
        }
        try:
            resp = client.post(
                f"{self._api_base}/bookings",
                params={"apiKey": self._api_key},
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # pragma: no cover - network path
            raise SchedulerBackendError(f"calcom_book_failed:{exc}") from exc

        event_id = str(data.get("id") or data.get("uid") or "")
        if not event_id:
            raise SchedulerBackendError("calcom_book_missing_event_id")
        return BookedEvent(provider_event_id=event_id, start=slot.start, end=slot.end)


# ---------------------------------------------------------------------------
# Google Calendar fallback backend
# ---------------------------------------------------------------------------


class GoogleCalendarBackend:
    """Fallback backend -- Google Calendar OAuth (personal calendars).

    Lazy-imports the ``google-auth`` / ``google-api-python-client``
    SDK chain inside the methods. When the OAuth refresh token is
    missing the constructor raises so the caller can fall back to a
    different backend or surface 503 from the router.
    """

    name = "google_calendar"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
    ) -> None:
        if not (client_id and client_secret and refresh_token):
            raise SchedulerBackendConfigError("google_calendar_oauth_missing")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._calendar_id = calendar_id

    def _service(self):
        try:
            from google.oauth2.credentials import Credentials  # type: ignore
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised via Config
            raise SchedulerBackendConfigError(
                f"google_calendar_sdk_unavailable:{exc}"
            ) from exc

        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def availability(
        self,
        *,
        partner_email: str,
        duration_min: int,
        window_start: datetime,
        window_end: datetime,
    ) -> List[Slot]:
        service = self._service()
        body = {
            "timeMin": _ensure_utc(window_start).isoformat(),
            "timeMax": _ensure_utc(window_end).isoformat(),
            "items": [{"id": self._calendar_id}],
        }
        try:
            resp = service.freebusy().query(body=body).execute()
        except Exception as exc:  # pragma: no cover - network path
            raise SchedulerBackendError(
                f"google_calendar_availability_failed:{exc}"
            ) from exc

        busy_ranges = []
        for cal in resp.get("calendars", {}).values():
            for b in cal.get("busy", []):
                start = datetime.fromisoformat(str(b["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(b["end"]).replace("Z", "+00:00"))
                busy_ranges.append((_ensure_utc(start), _ensure_utc(end)))
        busy_ranges.sort()

        slots = list(_iter_free_slots(
            window_start=_ensure_utc(window_start),
            window_end=_ensure_utc(window_end),
            busy=busy_ranges,
            duration=timedelta(minutes=duration_min),
        ))
        return _pick_top_three(slots, duration_min=duration_min)

    def book(
        self,
        *,
        slot: Slot,
        partner_email: str,
        founder_email: str,
        application_id: str,
    ) -> BookedEvent:
        service = self._service()
        body = {
            "summary": "Coherence Fund -- partner meeting",
            "description": f"Partner meeting for application {application_id}.",
            "start": {"dateTime": _ensure_utc(slot.start).isoformat()},
            "end": {"dateTime": _ensure_utc(slot.end).isoformat()},
            "attendees": [
                {"email": partner_email},
                {"email": founder_email},
            ],
        }
        try:
            event = (
                service.events()
                .insert(
                    calendarId=self._calendar_id,
                    body=body,
                    sendUpdates="all",
                )
                .execute()
            )
        except Exception as exc:  # pragma: no cover - network path
            raise SchedulerBackendError(
                f"google_calendar_book_failed:{exc}"
            ) from exc
        event_id = str(event.get("id") or "")
        if not event_id:
            raise SchedulerBackendError("google_calendar_book_missing_event_id")
        return BookedEvent(provider_event_id=event_id, start=slot.start, end=slot.end)


def _iter_free_slots(
    *,
    window_start: datetime,
    window_end: datetime,
    busy: Iterable[tuple[datetime, datetime]],
    duration: timedelta,
) -> Iterable[Slot]:
    """Yield non-overlapping ``duration``-long Slots in ``[window_start, window_end)``."""
    cursor = window_start
    for b_start, b_end in busy:
        while cursor + duration <= b_start and cursor + duration <= window_end:
            yield Slot(cursor, cursor + duration)
            cursor = cursor + duration
        if b_end > cursor:
            cursor = b_end
    while cursor + duration <= window_end:
        yield Slot(cursor, cursor + duration)
        cursor = cursor + duration


# ---------------------------------------------------------------------------
# In-memory backend (test-only)
# ---------------------------------------------------------------------------


class InMemorySchedulerBackend:
    """Deterministic in-memory backend used by tests.

    Holds a fixed availability list and produces a synthetic
    ``provider_event_id`` per :meth:`book` call. Booking the same
    slot twice raises :class:`SchedulerBackendError` so the
    scheduler's idempotency path can be exercised end-to-end.
    """

    name = "in_memory"

    def __init__(self, slots: Sequence[Slot]) -> None:
        self._slots: List[Slot] = list(slots)
        self._booked_starts: set[datetime] = set()
        self._next_id = 0

    def add_slots(self, slots: Iterable[Slot]) -> None:
        self._slots.extend(slots)

    def availability(
        self,
        *,
        partner_email: str,
        duration_min: int,
        window_start: datetime,
        window_end: datetime,
    ) -> List[Slot]:
        win_start = _ensure_utc(window_start)
        win_end = _ensure_utc(window_end)
        candidates = [
            s for s in self._slots
            if s.start >= win_start
            and s.end <= win_end
            and s.start not in self._booked_starts
        ]
        return _pick_top_three(candidates, duration_min=duration_min)

    def book(
        self,
        *,
        slot: Slot,
        partner_email: str,
        founder_email: str,
        application_id: str,
    ) -> BookedEvent:
        if slot.start in self._booked_starts:
            raise SchedulerBackendError("slot_already_booked")
        self._booked_starts.add(slot.start)
        self._next_id += 1
        return BookedEvent(
            provider_event_id=f"in-mem-{self._next_id:04d}",
            start=slot.start,
            end=slot.end,
        )


# ---------------------------------------------------------------------------
# Env factory
# ---------------------------------------------------------------------------


def scheduler_backend_from_env() -> SchedulerBackend:
    """Resolve the configured scheduling backend from environment variables.

    Selection rules:

    * ``SCHEDULER_PROVIDER=calcom`` -- :class:`CalComBackend`
    * ``SCHEDULER_PROVIDER=google_calendar`` -- :class:`GoogleCalendarBackend`
    * Any other value raises :class:`SchedulerBackendConfigError`.

    Cal.com is the documented default but we fall through with
    config-error rather than guessing so callers always know which
    backend they are talking to.
    """
    provider = os.getenv("SCHEDULER_PROVIDER", "").strip().lower() or "calcom"
    if provider == "calcom":
        return CalComBackend(
            api_key=os.getenv("CALCOM_API_KEY", ""),
            api_base=os.getenv("CALCOM_API_BASE", "https://api.cal.com/v1"),
            event_type_id=os.getenv("CALCOM_EVENT_TYPE_ID", ""),
        )
    if provider == "google_calendar":
        return GoogleCalendarBackend(
            client_id=os.getenv("GOOGLE_CALENDAR_OAUTH_CLIENT_ID", ""),
            client_secret=os.getenv("GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET", ""),
            refresh_token=os.getenv("GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN", ""),
            calendar_id=os.getenv("GOOGLE_CALENDAR_CALENDAR_ID", "primary"),
        )
    raise SchedulerBackendConfigError(f"unknown_scheduler_provider:{provider!r}")
