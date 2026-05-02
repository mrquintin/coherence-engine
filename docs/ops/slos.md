# Coherence Fund — Service Level Objectives (SLOs)

Wave 16, prompt 63/70. Defines the user-visible SLOs the Coherence
Fund platform reports against, the recording rules that materialize
each SLO's `error_ratio`, and the multi-window / multi-burn-rate
alerts that page on-call before the error budget is exhausted.

This document is the **single source of truth** for SLO numbers used
by `deploy/prometheus/recording_rules.yaml`,
`deploy/prometheus/alert_rules.yaml`, and the dashboards under
`deploy/grafana/dashboards/`. Per-stage targets in
`docs/ops/slo_metrics.md` and worker / outbox targets in
`docs/ops/slo_threshold_standards.md` remain authoritative for their
own surfaces; this file aggregates the four user-visible SLOs that
gate a release and feed the error-budget burn alerts.

## SLO catalog

| ID | Name | Target | Window | Signal source |
|----|------|--------|--------|---------------|
| `SLO_avail` | Decision API availability | **99.5 %** of `POST /api/v1/applications` requests succeed (non-5xx) | 30 days rolling | `http_requests_total{handler="/api/v1/applications",status_code}` |
| `SLO_latency` | Scoring completion latency | **P95 ≤ 30 s** end-to-end pipeline (intake → notify) | 30 days rolling | `coherence_fund_pipeline_stage_last_duration_seconds` (sum across stages) |
| `SLO_scoring_success` | Scoring job success rate | **≥ 99 %** scoring jobs succeed (`error_class != transient`) | 30 days rolling | `coherence_fund_pipeline_stage_events_total{stage="score",status}` |
| `SLO_calibration_freshness` | Calibration drift report freshness | Drift report regenerated **≤ 24 h** | continuous | `coherence_fund_calibration_drift_report_age_seconds` |

### Error budgets

| SLO | Allowed bad-event ratio (1 − target) | Monthly bad-minute budget (43 200 min) |
|-----|--------------------------------------|----------------------------------------|
| `SLO_avail` | 0.005 | 216 min |
| `SLO_latency` | 0.05 (P95 violations) | 2 160 min |
| `SLO_scoring_success` | 0.01 | 432 min |
| `SLO_calibration_freshness` | n/a (event-based) | n/a — alert fires once age > 24 h |

## Recording rules

`deploy/prometheus/recording_rules.yaml` materializes one
`error_ratio` and one `good_events / valid_events` series per SLO,
at the canonical short windows the burn-rate alerts join against
(5m, 30m, 1h, 2h, 6h, 24h, 72h). Naming follows
`coherence_fund:<slo_id>:error_ratio:<window>` so dashboards and
alerts can join without recomputing the underlying counter math.

The recording-rules group is named `coherence_fund_slo_recording_rules`
to make it discoverable from `promtool check rules` output and from
the Prometheus `/rules` endpoint.

## Burn-rate alerts (multi-window, multi-burn-rate)

Per the Google SRE workbook, *Implementing SLOs* chapter 5, raw
threshold alerts ("P95 > 30 s right now") alert on noise; instead we
join two windows at four burn rates so the alert fires only when the
SLO is genuinely at risk of being exhausted. Burn rate is defined
as `error_ratio / (1 − target)` — a burn rate of `1` exhausts the
30-day budget in exactly 30 days; a burn rate of `14.4` exhausts it
in ~2 days.

| Burn rate | Long window | Short window | Severity | Notification | Budget consumed if sustained |
|-----------|-------------|--------------|----------|--------------|------------------------------|
| 14.4× | 1h | 5m | `critical` | **page** | 2 % in 1h |
| 6× | 6h | 30m | `critical` | **page** | 5 % in 6h |
| 3× | 24h | 2h | `warning` | **ticket** | 10 % in 24h |
| 1× | 72h | 6h | `warning` | **ticket** | 10 % in 72h (steady drain) |

An alert fires only when **both** the long-window error ratio and
the short-window error ratio exceed `burn_rate × (1 − target)`.
The short window prevents an old burn from continuing to page after
the underlying issue is fixed; the long window suppresses one-off
spikes.

### Severity routing

* `severity: critical` → Alertmanager pager route → on-call.
* `severity: warning` → Alertmanager ticket route → backlog (next
  business day). **Never** page on slow burn — the whole point of
  multi-window / multi-burn-rate is that slow burns get tickets,
  not pages.

### Per-SLO alert names

| SLO | Fast-burn (page) | Medium (page) | Slow (ticket) | Very-slow (ticket) |
|-----|------------------|----------------|----------------|--------------------|
| `SLO_avail` | `CoherenceFundAvailabilityFastBurn` | `CoherenceFundAvailabilityMediumBurn` | `CoherenceFundAvailabilitySlowBurn` | `CoherenceFundAvailabilityVerySlowBurn` |
| `SLO_latency` | `CoherenceFundLatencyFastBurn` | `CoherenceFundLatencyMediumBurn` | `CoherenceFundLatencySlowBurn` | `CoherenceFundLatencyVerySlowBurn` |
| `SLO_scoring_success` | `CoherenceFundScoringFastBurn` | `CoherenceFundScoringMediumBurn` | `CoherenceFundScoringSlowBurn` | `CoherenceFundScoringVerySlowBurn` |
| `SLO_calibration_freshness` | n/a | n/a | `CoherenceFundCalibrationStale` (ticket) | n/a |

`SLO_calibration_freshness` is event-based, not ratio-based: it
fires a single ticket-level alert when
`coherence_fund_calibration_drift_report_age_seconds > 86400` for
30 minutes (debounce against a flap during a regeneration job).

## Dashboards

Three Grafana dashboards (schema 38, version-controlled JSON):

| File | Purpose |
|------|---------|
| `deploy/grafana/dashboards/decision_pipeline.json` | Decision pipeline SLO compliance (availability + latency + per-stage error ratio + burn-rate row). |
| `deploy/grafana/dashboards/scoring_layers.json` | Scoring layer success rate, layer latency, queue depth, anti-gaming alert rate. |
| `deploy/grafana/dashboards/cost_telemetry.json` | Per-application + daily cost rollups, budget headroom, cost-event throughput. |

Dashboards are stored as deterministic JSON (sorted keys, two-space
indent, LF newlines, trailing newline) so a re-export from Grafana
produces a clean diff or no diff at all. The validation test
`tests/test_grafana_json_validity.py` parses each file with `json`
and asserts the canonical row + panel IDs are present.

## Why not raw threshold alerts?

A raw threshold alert ("page if P95 > 30s for 5m") fires on every
deploy spike, every transient upstream wobble, and every cold-cache
restart. It does not distinguish a brief blip from a real budget
burn. Multi-window / multi-burn-rate gives:

* **Precision** — the long window suppresses spikes shorter than
  the short window.
* **Recall** — the fast-burn pair (1h / 5m at 14.4×) catches a
  large outage in minutes; the slow-burn pair (72h / 6h at 1×)
  catches a steady drain that a single-window alert would miss.
* **Reset speed** — the short window means the alert clears within
  the short-window length once the issue is fixed, instead of
  staying firing for the entire long window.

## References

* `deploy/prometheus/recording_rules.yaml`
* `deploy/prometheus/alert_rules.yaml`
* `deploy/grafana/dashboards/decision_pipeline.json`
* `deploy/grafana/dashboards/scoring_layers.json`
* `deploy/grafana/dashboards/cost_telemetry.json`
* `docs/ops/slo_metrics.md` — per-stage P50/P95 + error budgets.
* `docs/ops/slo_threshold_standards.md` — worker + outbox SLOs.
* Google SRE Workbook, chapter 5 — *Alerting on SLOs*.
