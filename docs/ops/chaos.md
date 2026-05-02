# Chaos test harness (operator runbook)

This runbook is the operator-facing companion to
[`deploy/chaos/run_scenario.py`](../../deploy/chaos/run_scenario.py).

The harness exercises the system against scripted **failover and
partition** events — DB primary failover, Redis flap, S3 outage, scoring
worker death mid-job — and asserts that the end-to-end pipeline still
completes within the stated SLO and that the determinism contract holds
(every decision artifact replays byte-identical).

It runs **locally**, against a docker-compose topology
([`deploy/chaos/docker-compose.yml`](../../deploy/chaos/docker-compose.yml)).
It is intentionally **not part of the default CI suite** — costs and
flakiness are too high to spin two Postgres containers + MinIO + a
worker per pull request. CI exercises only the dry-run path; live runs
are gated behind `CHAOS=1` and run by hand or on a nightly cron.

## When to run chaos scenarios

| Trigger | Cadence |
|---|---|
| Touched the scoring-job heartbeat / visibility-timeout path | Run `worker_death_mid_job.yaml` before merge |
| Touched DB connection / retry / pool config | Run `db_primary_failover.yaml` |
| Touched the ARQ enqueue / dequeue path | Run `redis_flap.yaml` |
| Touched the object-storage layer | Run `s3_outage.yaml` |
| Pre-release | Run all four; archive each `--json-out` report as release evidence |
| Nightly | Run all four; page on any non-zero exit code |

## Quick invocation

Dry-run (always safe; this is what CI runs):

```bash
python deploy/chaos/run_scenario.py \
  --scenario deploy/chaos/scenarios/db_primary_failover.yaml \
  --dry-run
```

Live run (requires Docker + `CHAOS=1`):

```bash
docker compose -f deploy/chaos/docker-compose.yml up --build -d
CHAOS=1 python deploy/chaos/run_scenario.py \
  --scenario deploy/chaos/scenarios/db_primary_failover.yaml \
  --json-out artifacts/chaos-db-failover.json
docker compose -f deploy/chaos/docker-compose.yml down -v
```

## Make target

Wire the harness to a `make chaos` target so the dry-run path is one
keystroke from the repo root. A reasonable target shape:

```make
.PHONY: chaos chaos-dry-run

chaos-dry-run:
	@for s in deploy/chaos/scenarios/*.yaml; do \
	  python deploy/chaos/run_scenario.py --scenario $$s --dry-run; \
	done

chaos: ## Live chaos run; requires Docker + CHAOS=1
	@if [ "$$CHAOS" != "1" ]; then \
	  echo "Refusing to run live chaos without CHAOS=1"; exit 3; fi
	docker compose -f deploy/chaos/docker-compose.yml up --build -d
	@for s in deploy/chaos/scenarios/*.yaml; do \
	  CHAOS=1 python deploy/chaos/run_scenario.py --scenario $$s \
	    --json-out artifacts/chaos-$$(basename $$s .yaml).json || \
	    (docker compose -f deploy/chaos/docker-compose.yml down -v; exit 1); \
	done
	docker compose -f deploy/chaos/docker-compose.yml down -v
```

## Scenarios shipped

| Scenario | Perturbation | Asserts |
|---|---|---|
| [`db_primary_failover.yaml`](../../deploy/chaos/scenarios/db_primary_failover.yaml) | `docker stop` + `start` Postgres primary | retry-with-backoff; no orphaned scoring jobs; byte-identical replay |
| [`redis_flap.yaml`](../../deploy/chaos/scenarios/redis_flap.yaml) | `pause` / `unpause` Redis twice | enqueue retries collapse to one job; byte-identical replay |
| [`s3_outage.yaml`](../../deploy/chaos/scenarios/s3_outage.yaml) | `docker stop` + `start` MinIO | retryable error → success on resume; byte-identical replay |
| [`worker_death_mid_job.yaml`](../../deploy/chaos/scenarios/worker_death_mid_job.yaml) | `docker stop` (SIGTERM, never SIGKILL) the worker | reaper promotes stale `in_progress` rows to `queued` exactly once; byte-identical replay |

### Why `docker stop`, never `docker kill -s 9`?

A `SIGKILL` on a worker that holds an open Postgres transaction risks
abandoning a row-level lock in the kernel's per-PID lock table beyond
what the chaos invariants cover. The prompt-30 worker-reliability
prohibitions explicitly forbid SIGKILL on processes that hold DB locks
without first observing lock release; chaos scenarios respect that.
The visibility-timeout reaper is exercised by the SIGTERM path —
it's the same code path a real container restart hits.

## Scenario YAML schema (`chaos-scenario-v1`)

```yaml
schema_version: chaos-scenario-v1
name: <slug>
description: |
  Free-form, why this scenario exists.
slo:
  end_to_end_seconds: <int>
pre_state:
  required_services: [<docker-compose service names>]
  startup_timeout_seconds: <int>
perturbation:
  - action: stop | start | pause | unpause | partition
    target: <docker-compose service name>
    duration_seconds: <int>           # optional
    signal: SIGTERM                   # optional, only for stop
    timeout_seconds: <int>            # optional, only for stop
    note: <string>                    # optional, free-form
workload:
  kind: synthetic_application_submit
  count: <int>
  application_fixture: <path-relative-to-repo-root>
  wait_for_completion_timeout_seconds: <int>
post_conditions:
  - kind: no_orphaned_scoring_jobs
          | idempotency_intact
          | byte_identical_artifact_replay
          | end_to_end_within_slo
    detail: <string>                  # optional
```

The validator enforces one **load-bearing invariant**: every scenario
must declare the `byte_identical_artifact_replay` post-condition. This
is the **determinism contract** — a chaos run that doesn't re-derive
byte-identical artifact bytes from the persisted inputs has not
verified the property that actually matters.

## Reading the post-condition output

`run_scenario.py --json-out <path>` writes a stable report shape:

```json
{
  "ok": true,
  "scenario": "db_primary_failover",
  "schema_version": "chaos-scenario-v1",
  "mode": "live" | "dry_run",
  "perturbation_steps": 2,
  "workload": {
    "submitted": 3,
    "completed": 3,
    "wall_clock_seconds_p95": 17.4,
    "applications": ["...application ids..."]
  },
  "post_conditions": [
    {"kind": "no_orphaned_scoring_jobs", "ok": true, "detail": ""},
    {"kind": "byte_identical_artifact_replay", "ok": true, "detail": "sha256 match on 3/3 applications"}
  ]
}
```

A run is green iff `ok: true` AND every `post_conditions[].ok` is true.
Archive the JSON alongside the release-readiness JSON as part of the
release evidence pack.

### When a post-condition fails

* `no_orphaned_scoring_jobs: false` → look at the `scoring_jobs` rows
  for the affected applications; the visibility-timeout reaper either
  isn't running or isn't eligible to claim them.
* `idempotency_intact: false` → there's a duplicate
  `scoring_jobs` row keyed on the application's idempotency hash. The
  enqueue path is letting two-write retries through.
* `byte_identical_artifact_replay: false` → **stop the line**. This is
  a determinism regression. Compare the two artifact byte-streams;
  the diff identifies the non-deterministic field (usually a
  timestamp, a non-stable sort, or an unpinned model version).
* `end_to_end_within_slo: false` → the pipeline made forward progress
  but blew the SLO budget; this is a flakiness signal, not a
  correctness one.

## Prohibitions (load-bearing)

* **Never** run chaos scenarios in default CI. Gate behind `CHAOS=1`.
  Cost + flakiness make per-PR runs unviable.
* **Never** skip the `byte_identical_artifact_replay` post-condition.
  It's the determinism contract; the validator rejects scenarios that
  forget it.
* **Never** use `kill -9` (SIGKILL) on processes that hold DB locks
  without first observing lock release. Use `docker stop` (SIGTERM,
  10s grace) — the visibility-timeout reaper covers the same recovery
  path without abandoning row locks.
