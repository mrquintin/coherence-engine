# Pipeline stage SLO metrics (fund workflow orchestrator)

This catalog defines the per-stage SLO surface emitted by the fund
workflow orchestrator (`server/fund/services/workflow.py`, prompt 15
of 20) via the pipeline telemetry helper
(`record_stage` in `server/fund/services/ops_telemetry.py`,
prompt 18 of 20). Numbers are **starting points**; tune them using
observed traffic and error budgets before production rollout.

The companion document `docs/ops/slo_threshold_standards.md` continues
to own scoring-worker and outbox-dispatcher SLOs; this file is purely
additive and does **not** change those semantics.

## Signal sources

* **Structured logs** — every `record_stage(...)` call emits a
  single log line tagged with the marker
  `COHERENCE_FUND_PIPELINE_STAGE_EVENT`. The JSON payload carries
  `stage`, `status`, `duration_s`, optional `warn` tags, and an
  optional `extra` object with `application_id`, `workflow_run_id`,
  `workflow_step_id`, and `trace_id`.
* **JSONL append sink** (optional) — enabled by
  `COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH`. One JSON object per
  stage event, newline-delimited. Disjoint from the worker-ops JSONL
  file so you can opt into each surface independently.
* **Prometheus textfile sink** (optional) — enabled by
  `COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH`. Written
  atomically and scraped via the node_exporter textfile collector or
  equivalent. Metric families:
  * `coherence_fund_pipeline_stage_events_total{stage,status}` (counter)
  * `coherence_fund_pipeline_stage_last_duration_seconds{stage}` (gauge)
  * `coherence_fund_pipeline_stage_duration_seconds_sum{stage}` (counter)
  * `coherence_fund_pipeline_stage_warn_total{stage}` (counter)

## Canonical stage identifiers

The orchestrator executes nine stages in the order below. The
left-hand column is the **canonical `stage` label** written to every
telemetry event and Prometheus series; the right-hand column is the
human-friendly name used in runbooks, dashboards, and cross-references
to other modules.

| Canonical `stage` | Descriptive name | Upstream reference |
|-------------------|------------------|--------------------|
| `intake` | Application intake | `_stage_intake` |
| `transcript_quality` | Transcript quality gate | `_stage_transcript_quality` |
| `compile` | Transcript + argument compile (transcript_compile) | `_stage_compile` |
| `ontology` | Ontology extract (ontology_extract) | `_stage_ontology` |
| `domain_mix` | Domain mix layering | `_stage_domain_mix` |
| `score` | Coherence scoring fan-out | `_stage_score` |
| `decide` | Decision policy evaluation | `_stage_decide` |
| `artifact` | Decision artifact persistence (decision_artifact) | `_stage_artifact` |
| `notify` | Notification dispatch (notification_dispatch) | `_stage_notify` |

The prompt-18 verification surface mentions several **stage aliases**:
`transcript_compile` → `compile`, `ontology_extract` → `ontology`,
`decision_artifact` → `artifact`, `notification_dispatch` → `notify`.
These are documented here so grep queries targeting either vocabulary
land on this catalog.

## Latency and error-rate targets

| Stage | P50 latency (seconds) | P95 latency (seconds) | Error-rate budget (15m) | Notes |
|-------|-----------------------|------------------------|--------------------------|--------|
| `intake` | < 0.1 | < 0.5 | 0.1 % | Pure DB fetch + field projection. Any failure implies a missing `Application` row. |
| `transcript_quality` | < 0.1 | < 0.5 | 0.5 % | Fails when the transcript is empty or compliance is `blocked`. Failures are usually a data-ingest bug, not a worker-capacity issue. |
| `compile` (transcript_compile) | < 1.5 | < 5.0 | 0.5 % | Writes an ArgumentArtifact row and publishes two outbox events; bounded by SQLite / Postgres round-trips. |
| `ontology` (ontology_extract) | < 0.1 | < 0.5 | 0.5 % | Ontology version + contradiction count extraction from an in-memory score blob. |
| `domain_mix` | < 0.1 | < 0.5 | 0.5 % | Layer key projection from the score blob. |
| `score` | < 2.0 | < 10.0 | 0.5 % | Publishes `CoherenceScored`; cost scales with number of propositions. |
| `decide` | < 0.5 | < 2.0 | 0.2 % | Evaluates decision policy; failures usually mean a missing scoring prereq. |
| `artifact` (decision_artifact) | < 0.5 | < 2.0 | 0.2 % | Builds the canonical `decision_artifact.v1` and persists it. |
| `notify` (notification_dispatch) | < 1.0 | < 5.0 | 0.5 % | Dispatches founder + partner notifications via the configured backend. |

End-to-end workflow (`sum(stage_duration)`) should stay under **P95
≈ 15 seconds** on a warm Postgres; cold-cache or multi-region clusters
should budget P95 ≈ 30 seconds. The end-to-end error budget is the
**per-run success ratio**: target ≥ 99.5 % over a rolling 15-minute
window, measured from `coherence_fund_pipeline_stage_events_total`
(`status="success"` at stage `notify` divided by `status="success"` at
stage `intake`).

## In-process warning env vars (safe defaults)

| Variable | Safe default | Example production starting point |
|----------|--------------|-----------------------------------|
| `COHERENCE_FUND_PIPELINE_STAGE_DURATION_WARN_SECONDS` | `0` (disabled) | `5.0` |
| `COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH` | _unset_ | `/var/log/coherence-fund/pipeline_stages.jsonl` |
| `COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH` | _unset_ | `/var/lib/node_exporter/textfile_collector/coherence_fund_pipeline.prom` |

Setting any of these to their safe default disables the corresponding
sink. The warn-seconds threshold adds a `warn=duration_budget` tag to
the emitted event **and** bumps the
`coherence_fund_pipeline_stage_warn_total{stage=...}` counter, which is
what the alert rules in
`deploy/helm/templates/prometheus-rules-pipeline.yaml` and
`deploy/k8s/prometheus/rules-pipeline.yaml` page on.

## Error budget and escalation (recommended)

* **Warning**: sustained approach to a P95 latency target (15 minutes)
  or a non-zero `coherence_fund_pipeline_stage_warn_total{stage=...}`
  counter — ticket, scale, or investigate during business hours.
* **Critical**: any stage `status="failure"` burn-rate above **1 %
  over 15 minutes** — page on-call, and correlate with
  `notify` failures + notification dry-run output before assuming a
  worker-capacity issue.
* **Post-incident**: update this document and the rule-stub `expr` /
  `for` durations together so runbooks stay accurate.

## References

* `server/fund/services/ops_telemetry.py` — `record_stage` emitter.
* `server/fund/services/workflow.py` — stopwatch-wrapped step loop.
* `docs/ops/slo_threshold_standards.md` — scoring + outbox SLOs.
* `docs/ops/runbooks/production_observability_rollout.md` — rollout
  and on-call routing for the combined worker + pipeline surface.
* `deploy/helm/templates/prometheus-rules-pipeline.yaml` — chart-managed
  rule stubs (gated behind `prometheusRules.enabled`).
* `deploy/k8s/prometheus/rules-pipeline.yaml` — raw `PrometheusRule`
  manifest for clusters that do not use Helm.
