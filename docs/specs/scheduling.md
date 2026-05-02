# Partner-meeting scheduling (prompt 54)

This document specifies the calendar / scheduling adapter that
auto-books partner meetings when an enforce-mode `pass` decision is
issued. The implementation lives in:

- `server/fund/services/scheduler.py`           — `Scheduler`, `propose`/`book`, errors
- `server/fund/services/scheduler_backends.py`  — `CalComBackend`, `GoogleCalendarBackend`, `InMemorySchedulerBackend`
- `server/fund/routers/scheduling.py`           — `POST /scheduling/proposals`, `POST /scheduling/book`
- `server/fund/models.py`                       — `MeetingProposal`, `Booking`
- `alembic/versions/20260425_000012_scheduling.py`

## Why

After a founder receives a `pass` decision the next required step is
a partner meeting. Asking them to email-tag a partner and play
calendar Tetris is the dominant cause of drop-off between
"approved" and "first money in". The scheduler closes that gap by
proposing three slots automatically and booking on click.

## Lifecycle

```
pass decision (enforce mode)
    -> ApplicationService emits scheduling_requested  (outbox)
        -> Scheduler.propose(application_id, partner_email)
            -> backend.availability(...)
            -> persist MeetingProposal {token, top-3 slots, expires_at}
            -> render founder URL: <booking_url_base>?token=<token>
        -> founder click-through with chosen slot
            -> Scheduler.book(token, chosen_slot_start)
                -> validate token not expired (HTTP 410 on expired)
                -> backend.book(...)  -> provider_event_id
                -> persist Booking, mark proposal "booked"
                -> events.publish("meeting_booked", ...)
```

## Backends

Two pluggable backends sit behind the `SchedulerBackend` protocol:

| Backend                    | Use case                              | Env vars                                                                                          |
|----------------------------|---------------------------------------|---------------------------------------------------------------------------------------------------|
| `CalComBackend` (primary)  | Partners with managed Cal.com accounts| `CALCOM_API_KEY`, `CALCOM_API_BASE`, `CALCOM_EVENT_TYPE_ID`                                       |
| `GoogleCalendarBackend`    | Personal calendars (OAuth)            | `GOOGLE_CALENDAR_OAUTH_CLIENT_ID`, `GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET`, `GOOGLE_CALENDAR_OAUTH_REFRESH_TOKEN`, `GOOGLE_CALENDAR_CALENDAR_ID` |

Selection is via `SCHEDULER_PROVIDER` (defaults to `calcom`). Both
SDK chains are imported lazily inside the methods so the import
cost is paid only when the backend is actually used (prompt 54
prohibition: do NOT make either provider a hard dependency).

A test-only `InMemorySchedulerBackend` powers
`tests/test_scheduler.py`; no real network calls happen in CI.

## Slot bandwidth

`MAX_PROPOSED_SLOTS = 3`. The helper `_pick_top_three` collapses
duplicate starts and hard-caps the proposal at three even when the
backend returns more candidates. Founder-facing UX testing
consistently shows three options as the sweet spot — fewer reads as
"no time", more reads as decision paralysis.

## Token model

- 32 hex characters (16 bytes from `secrets.token_hex`).
- Stored in `MeetingProposal.token`, uniqueness-enforced by index
  `ix_fund_meeting_proposals_token`.
- TTL is `DEFAULT_PROPOSAL_TTL_HOURS = 72` hours (configurable via
  `Scheduler(proposal_ttl=...)`).
- Expiry is checked in `Scheduler.book`; an expired token raises
  `TokenExpiredError`, which the route surfaces as **HTTP 410 GONE**.
- Prompt 54 prohibition: there is **no env-gated bypass** of the
  expiry check. Operator-driven re-issue creates a brand-new
  proposal row, never extends the existing one.

## Idempotency

A second `book(token, chosen_slot_start)` call with the same token
after a successful booking returns the existing `Booking` row
unchanged with `idempotent_replay=True`. The backend is not called
twice. The route surfaces this as a 200 (not 201 / 409) so
duplicate-tab clicks are silent rather than user-visible failures.

## Storage discipline

Proposed slots are persisted as a JSON-encoded `Text` blob (NOT
JSONB) so the same migration runs unmodified against the SQLite
test fixture and Postgres staging/prod clusters. Provider event
identifiers are persisted as opaque strings; raw Cal.com / Google
Calendar payloads never enter the database.

## Events

| Event                  | Producer    | Trigger                                  | Idempotency key                              |
|------------------------|-------------|------------------------------------------|----------------------------------------------|
| `scheduling_requested` | `scheduler` | Pass decision in enforce mode            | `<scoring_job.idempotency_key>:SchedulingRequested` |
| `meeting_booked`       | `scheduler` | Successful `Scheduler.book`              | `meeting_booked:<booking_id>`                |

Both events ride the existing `EventPublisher` outbox; no new
worker is required.

## Failure modes & HTTP mapping

| Condition                              | Class                       | HTTP   | Code                  |
|----------------------------------------|-----------------------------|--------|------------------------|
| Backend SDK / env config missing       | `SchedulerBackendConfigError` | 503    | `PROVIDER_UNAVAILABLE` |
| Backend network / API failure          | `SchedulerBackendError`     | 502    | `PROVIDER_ERROR`       |
| Token does not exist                   | `ProposalNotFoundError`     | 404    | `NOT_FOUND`            |
| Token past `expires_at`                | `TokenExpiredError`         | 410    | `GONE`                 |
| Proposal cancelled by operator         | `ProposalCancelledError`    | 409    | `CONFLICT`             |
| Validation (missing field, bad slot)   | `SchedulerError`            | 400    | `VALIDATION_ERROR`     |

## Operator obligations

- Configure `SCHEDULER_DEFAULT_PARTNER_EMAIL` (or wire the
  partner-routing surface, when it lands) so the
  `scheduling_requested` event always carries a non-empty address.
- Rotate `CALCOM_API_KEY` per Cal.com's account-key rotation
  guidance; the env var is the single source of truth and the
  backend constructor reads it on each instantiation.
- For Google Calendar, the OAuth refresh token MUST be issued
  against a service account or a dedicated booking-bot user — not a
  personal partner account.
