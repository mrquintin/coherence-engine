# SLO threshold standards (fund workers)

This document defines **starting-point** service levels for outbox dispatcher and scoring queue workers. Treat numbers as baselines: tune using historical traffic, broker latency, and error budgets for your environment.

## Signal sources

- **Logs / JSONL**: ops snapshots with prefix `COHERENCE_FUND_WORKER_OPS_SNAPSHOT` and optional `warn` array when in-process thresholds fire.
- **Prometheus** (optional): gauges written via `COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH` and scraped from the node_exporter textfile collector (or equivalent). Metric names are stable; see `docs/ops/README.md`.

## Availability and freshness (worker processing)

| Objective | Scoring worker | Outbox dispatcher | Notes |
|-----------|----------------|-------------------|--------|
| Backlog depth (eligible / dispatchable) | P99 over 15m < 100 jobs | P99 over 15m < 5,000 rows | Scale workers or investigate stuck claims when sustained. |
| Oldest waiting item age | P99 over 15m < 30 minutes | P99 over 15m < 60 minutes | Aligns with common `*_OPS_OLDEST_WARN_SECONDS` examples in env templates. |
| Terminal failures (DLQ-style counters) | Steady state near 0; page if > 5 sustained 10m | Steady state near 0; page if > 10 sustained 10m | Investigate root cause; DLQ growth is an error-budget burn. |
| Tick activity | Non-zero `tick_processed` / `tick_published` during business traffic windows | Same for `tick_published` | Use recording rules or Grafana to detect “flat” series alongside depth. |

## In-process warning env vars (safe defaults)

Setting any of these to **0** disables that warning (default behavior). Non-zero values add log/JSON `warn` tags and set the `coherence_fund_worker_ops_warn_*` gauges to 1 when breached.

| Variable | Safe default | Example production starting point |
|----------|--------------|-----------------------------------|
| `COHERENCE_FUND_SCORING_OPS_QUEUE_WARN_DEPTH` | `0` | `100` |
| `COHERENCE_FUND_SCORING_OPS_OLDEST_WARN_SECONDS` | `0` | `1800` |
| `COHERENCE_FUND_SCORING_OPS_FAILED_DLQ_WARN_COUNT` | `0` | `5` |
| `COHERENCE_FUND_OUTBOX_OPS_QUEUE_WARN_DEPTH` | `0` | `5000` |
| `COHERENCE_FUND_OUTBOX_OPS_OLDEST_WARN_SECONDS` | `0` | `3600` |
| `COHERENCE_FUND_OUTBOX_OPS_FAILED_DLQ_WARN_COUNT` | `0` | `10` |

Prometheus alerting should still be configured for clusters where logs alone are insufficient; keep **Helm** or **raw** `PrometheusRule` assets in sync with the numbers you adopt here.

## Error budget and escalation (recommended)

- **Warning**: sustained approach to threshold (for example 15–30 minutes) — ticket, scale, or investigate during business hours.
- **Critical**: DLQ counters rising, or depth/age far beyond table above for **30+ minutes** — page on-call.
- **Post-incident**: update this document and the alert rule `expr`/`for` durations together so runbooks stay accurate.

Keep **PagerDuty escalation policies**, **Opsgenie escalation policies**, and **Alertmanager receiver names** aligned with the per-environment registry in `deploy/ops/oncall-route-policy.example.json` (copy to your private policy file). Verify structure with `deploy/scripts/verify_oncall_route_policy.py`; attach workflow artifact `release-readiness-summary.md` or drill JSONL to releases as evidence when appropriate.

## References

- `docs/ops/README.md` — metric names and sinks.
- `docs/ops/runbooks/production_observability_rollout.md` — rollout, routing, and **scheduled verification cadence** (including `.github/workflows/oncall-route-verification.yml`).
- `deploy/k8s/alerts/fund-worker-slo-rules.yaml` — optional raw PrometheusRule manifest.
- `deploy/helm/coherence-fund/templates/prometheus-rules.yaml` — optional chart-managed rules (`prometheusRules.enabled`).
