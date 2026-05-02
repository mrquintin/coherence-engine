# Database connection pooling, read-replica routing, transient-error retries

This document describes three related production-readiness concerns for
the fund backend, all introduced in Wave 7 prompt 23:

1. **Pooling** — how SQLAlchemy talks to Postgres directly vs. through
   the Supabase pgbouncer pooler.
2. **Read-replica routing** — when reads can be safely served from a
   replica, and how that routing falls back to the primary.
3. **Transient-error retries** — which DB errors are safe to retry, and
   the bounded backoff used to do so.

The defaults are conservative: with no environment configuration, the
backend uses SQLite, no replica, and no retry — so `python -m pytest`
and local development are unchanged.

## Pooling

Engine kwargs are dialect-aware. See
`server/fund/database.py::_engine_kwargs_for_url`:

| Target                     | Pool                                    | Notes                                                            |
| :------------------------- | :-------------------------------------- | :--------------------------------------------------------------- |
| SQLite                     | (default `QueuePool`)                   | `connect_args={"check_same_thread": False}`                      |
| Direct Postgres            | `pool_size=10, max_overflow=20`         | `pool_pre_ping=True, pool_recycle=1800`                          |
| Supabase pgbouncer pooler  | `NullPool`                              | pgbouncer is the pool — layering SQLAlchemy on top corrupts state |

The pgbouncer recognition is automatic: any URL whose host contains
`pooler` or whose query string sets `pgbouncer=true` gets `NullPool`.

## Read-replica routing

When `SUPABASE_DB_REPLICA_URL` is set, the backend builds a second
engine pointing at the replica with **half** the primary's pool budget
(direct-Postgres only — NullPool / SQLite kwargs are unchanged). The
replica engine inherits `pool_pre_ping=True`.

Repositories opt into the replica per-call:

```python
state = portfolio_repo.latest_state(read_only=True)
```

Internally, `read_only=True` resolves a session from
`SessionFactory(read_only=True)` for the duration of the call and closes
it on exit. When no replica is configured, the same call transparently
runs against the primary engine — there is no second code path to
maintain.

### Replica SLA — what is safe to read

Replicas in Supabase / vanilla Postgres are **asynchronously**
replicated. Replication lag is normally well under a second but can
spike to tens of seconds during heavy-write windows. Treat the lag
budget as **30 seconds** for the purposes of this backend.

| Data class                                                            | Safe on replica? | Rationale                                                                |
| :-------------------------------------------------------------------- | :--------------: | :----------------------------------------------------------------------- |
| Application transcripts (`fund_applications.transcript_text` / `_uri`) | YES              | Written once, read many times. Lag is invisible to consumers.            |
| Decisions older than 30s (`fund_decisions`)                            | YES              | Once issued they are immutable.                                          |
| Argument artifacts (`argument_artifacts`)                              | YES              | Append-only; readers tolerate a small lag.                               |
| Portfolio state snapshots more than one tick old                       | YES              | Snapshot rows are append-only; current = max(as_of).                     |
| Just-written application state ("read-your-writes")                    | NO               | Reader expects to see what the same logical request just wrote.          |
| Scoring-job lease state (`fund_scoring_jobs`)                          | NO               | Worker-claim correctness depends on the latest committed lease.          |
| Idempotency record lookups (`fund_idempotency_records`)                | NO               | Replication lag turns a duplicate POST into a double-effect.             |
| Outbox dispatch (`fund_event_outbox`)                                  | NO               | Lag here causes double-publish of events.                                |

### Stale-read protection

The split is enforced **by call site**: a method that needs read-your-
writes simply omits `read_only=True` (or omits the `session=` override),
and SQLAlchemy uses the primary session passed to the repository
constructor. The package never silently routes a write to the replica —
write methods call `_require_primary()` which raises if the constructor
session is `None`.

For correctness, application-level code that reads after writing in the
same logical operation MUST use the primary session. The replica is for
read-mostly endpoints (list views, history queries, dashboards).

## Transient-error retries

`server/fund/database.py::retry_transient_db_errors` is a decorator
factory that retries a small, well-known class of transient SQLAlchemy
errors with bounded exponential backoff and full jitter.

### Classification

```
Retryable:
    sqlalchemy.exc.OperationalError
    sqlalchemy.exc.DBAPIError when connection_invalidated == True
    sqlalchemy.exc.DBAPIError with Postgres SQLSTATE in:
        40001  serialization_failure
        40P01  deadlock_detected
        57P01  admin_shutdown / server going away

Never retried:
    sqlalchemy.exc.IntegrityError   (logic bug — uniqueness, FK, NOT NULL)
    sqlalchemy.exc.DataError        (logic bug — invalid value)
    Anything that is not a DBAPI / Operational error
```

Retrying `IntegrityError` only hides bugs and delays detection; a
duplicate insert is not "transient", it is wrong.

### Backoff

Full jitter, capped:

```
delay_ms = uniform(0, min(max_delay_ms, base_delay_ms * 2**(attempt - 1)))
```

Defaults:

| Setting                                   | Default | Env var                                  |
| :---------------------------------------- | :-----: | :--------------------------------------- |
| `max_attempts`                            |   `4`   | `COHERENCE_FUND_DB_RETRY_MAX_ATTEMPTS`   |
| `base_delay_ms`                           |  `50`   | `COHERENCE_FUND_DB_RETRY_BASE_DELAY_MS`  |
| `max_delay_ms`                            | `2000`  | `COHERENCE_FUND_DB_RETRY_MAX_DELAY_MS`   |

`max_delay_ms` is a **hard cap** — there is no path to unbounded
backoff. The decorator validates that `max_delay_ms >= base_delay_ms`
at construction time.

### Determinism in tests

Both the sleeper and the RNG are injectable:

```python
@retry_transient_db_errors(
    max_attempts=4,
    base_delay_ms=50,
    max_delay_ms=2000,
    sleeper=fake_sleeper,   # callable taking seconds-as-float
    rng=fake_rng,           # object with .uniform(a, b)
)
def f(): ...
```

The default RNG is `secrets.SystemRandom`, NOT `random.random()` —
collision-safe under contention is what we want for backoff jitter.

### Logging

Every retry emits a structured `db.retry.attempt` log at WARNING with
`attempt`, `error_class`, `delay_ms`, and `function` extras. On
exhaustion (last attempt fails), the decorator emits `db.retry.exhausted`
at ERROR with `attempts`, `error_class`, and `last_error_message` (first
500 chars).

### Where it's applied

The scoring worker's job-claim and job-finish paths are the primary
production users:

* `server/fund/scoring_worker.py::claim_next_job` — direct decorated
  helper that wraps `ApplicationRepository.claim_next_scoring_job`.
* `server/fund/scoring_worker.py::mark_job_completed` — wraps
  `ApplicationRepository.mark_scoring_job_completed`.
* `server/fund/scoring_worker.py::_wrap_repository_with_retry` — applies
  the same decorator per-instance to the repository handed to
  `ApplicationService`, so the service's internal calls inherit the
  same retry budget.

API request handlers do NOT inherit this decorator. A 500 returned to
the client because of a deadlock is preferable to extending request
latency by 2 seconds — the client retries via HTTP semantics, which
include the request body.
