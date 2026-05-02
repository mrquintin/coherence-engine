# Background Worker Runbook (Arq + Redis)

This runbook covers operating the fund orchestrator's background worker
layer. Two backends are supported and selected by the `WORKER_BACKEND`
environment variable:

| Backend | Selector | Discovery | Failsafe |
|---------|----------|-----------|----------|
| Polling | `WORKER_BACKEND=poll` (default) | DB rows (`ScoringJob`, `EventOutbox`) | Always available |
| Arq     | `WORKER_BACKEND=arq`             | Redis queue (Arq jobs)               | Requires Redis  |

The polling worker remains the canonical failsafe — flipping back to it
is a single env-var change and a service restart, no code deploy.

## When to use which backend

- **Polling**: local dev, CI, environments without Redis, or when Redis
  is unhealthy (failover). DB load is bounded by the polling cadence
  (`--poll-seconds`); steady-state load is very low.
- **Arq**: production where job latency must be sub-second. Arq
  schedules jobs immediately on enqueue and the worker reacts via a
  Redis blocking pop, which avoids the polling worker's worst-case
  `poll_seconds` delay.

## Components

```
server/fund/workers/
├── __init__.py        — package facade re-exporting helpers
├── tasks.py           — pure-function task units shared by both backends
│                          run_scoring_job(application_id) -> dict
│                          dispatch_outbox_batch(limit) -> int
│                          run_backtest_async(config) -> dict
├── dispatch.py        — enqueue helpers (no-op on poll backend)
│                          enqueue_scoring_job(application_id, idempotency_key)
│                          enqueue_outbox_dispatch(limit)
│                          enqueue_backtest(config, idempotency_key)
└── arq_worker.py      — Arq WorkerSettings + async stubs + main()
```

## Starting the worker

### Arq backend

```bash
# Direct entrypoint (matches systemd ExecStart):
WORKER_BACKEND=arq REDIS_URL=redis://localhost:6379/0 \
    python -m coherence_engine.server.fund.workers.arq_worker

# Equivalent via the Arq CLI (handy for ad-hoc local runs):
arq coherence_engine.server.fund.workers.arq_worker.WorkerSettings
```

The worker will log `arq_worker.startup queue_prefix=coherence_fund
redis=redis://***@host:port/db` on first connect. Watch for
`arq.score_job.start` / `arq.score_job.done` lines per job.

### Poll backend (failsafe)

```bash
WORKER_BACKEND=poll \
    python -m coherence_engine.server.fund.scoring_worker --run-mode loop
WORKER_BACKEND=poll \
    python -m coherence_engine.server.fund.worker --backend redis --redis-url redis://... --run-mode loop
```

## Redis connection requirements

Required: a Redis 6.x or 7.x endpoint reachable from the worker host.
The DSN must encode credentials and TLS posture:

```
redis://[:password@]host:port/db
rediss://[:password@]host:port/db   # TLS
```

The Arq pool reuses a single connection across the worker process. The
default `RedisSettings.from_dsn(url)` honors `rediss://` for TLS.

### Upstash Redis quirks

Upstash Redis is the recommended managed Redis for Supabase-paired
deployments. Two transport modes are exposed:

- **TLS (preferred)**: `rediss://default:<password>@<endpoint>:6379`.
  Use this with Arq — it speaks RESP over TLS just like vanilla Redis.
  No code changes needed.
- **REST API**: HTTP-based, used by the `@upstash/redis` JS client.
  Arq does **not** speak this. Do not point `REDIS_URL` at the REST
  endpoint.

Upstash's free tier ships with a per-day request budget; verify your
plan covers your peak enqueue+poll rate before promoting to prod.

## Restart vs. drain

### Graceful drain (Arq)

```bash
# Send SIGTERM. Arq's run_worker handles SIGTERM/SIGINT by:
#   1. stopping the queue listener (no new jobs are picked up)
#   2. waiting up to job_timeout (900s) for in-flight jobs to finish
#   3. flushing the result store
systemctl stop coherence-fund-arq-worker
```

The systemd unit declares `TimeoutStopSec=950` (slightly above
`job_timeout`) so the kernel does not SIGKILL during drain.

### Hard restart

```bash
systemctl restart coherence-fund-arq-worker
```

Use this for config changes (e.g., bumping `max_jobs`). In-flight jobs
that exceed `job_timeout` will be retried (`max_tries=3`); after
exhaustion, the underlying `ScoringJob` row is moved to `failed`.

## Re-enqueueing a stuck job

A "stuck" job is one in `processing` state on the `ScoringJob` table
whose `lease_expires_at` is in the past. Both backends will re-claim
it on the next pass (the polling worker's claim clause and the Arq
side's `run_scoring_job` both use the same lease-expiry logic). To
force a faster recovery on the Arq backend, you can re-enqueue
explicitly:

```python
import asyncio
from coherence_engine.server.fund.workers.dispatch import enqueue_scoring_job

asyncio.run(
    enqueue_scoring_job("app_<uuid>", idempotency_key="manual-rescue-1")
)
```

Idempotency: the Arq `_job_id` is `score:<application_id>:<key>`. Use a
unique `idempotency_key` for each rescue attempt or the second call
will be deduplicated by Arq.

## Observability

The polling and Arq worker both call into `tasks.run_scoring_job` /
`dispatch_outbox_batch`, so the existing ops snapshots
(`emit_scoring_ops_snapshot`, `emit_outbox_ops_snapshot`) cover both
backends without changes. Watch for these markers in logs:

- `worker_ops_snapshot` (component=`scoring` or `outbox`)
- `arq.score_job.start` / `arq.score_job.done`
- `arq_worker.startup` / `arq_worker.shutdown`

## Failure handling

- Arq retries up to `max_tries=3` with exponential backoff. On terminal
  failure the synchronous `run_scoring_job` body marks the underlying
  `ScoringJob` row `failed` (via `service.process_next_scoring_job` →
  `fail_or_retry_scoring_job`). The Arq result store keeps the error
  context for `keep_result=86400` seconds (24h).
- A Redis outage on the request side is harmless: the
  `enqueue_scoring_job` call should always be wrapped in
  `BackgroundTasks` so the request returns even if Redis is down. The
  polling worker is still draining `ScoringJob` rows from the database
  in parallel — flipping `WORKER_BACKEND=poll` while Redis is out is a
  valid recovery path.

## Configuration reference

| Env var | Default | Description |
|---------|---------|-------------|
| `WORKER_BACKEND` | `poll` | `poll` or `arq` |
| `REDIS_URL` | `redis://localhost:6379/0` | Arq DSN |
| `ARQ_QUEUE_PREFIX` | `coherence_fund` | Queue-name prefix |

WorkerSettings tunables (subclass to override):

| Attribute | Default | Notes |
|-----------|---------|-------|
| `max_jobs` | 4 | Bound to keep CPU-heavy scoring from starving outbox |
| `max_tries` | 3 | Per-job retry budget |
| `job_timeout` | 900 | 15-minute wall-clock cap per job |
| `keep_result` | 86400 | 24-hour result retention |
| `poll_delay` | 0.5 | Idle pop delay (Arq internal) |
