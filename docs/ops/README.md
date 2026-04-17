# Operations: worker telemetry and SLOs

Fund workers emit periodic **ops snapshots** (JSON, log line prefix `COHERENCE_FUND_WORKER_OPS_SNAPSHOT`) for queue depth, ages, DLQ counts, and per-tick activity. Centralized sinks are configured in `server/fund/services/ops_telemetry.py`.

## Environment variables

| Variable | Purpose |
|----------|---------|
| (none) | **Log sink** is always enabled: same marker and `info`/`warning` level as before. |
| `COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH` | Append each snapshot as one JSON line (JSONL). No network. |
| `COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH` | Overwrite a `.prom` file for the [node_exporter textfile collector](https://github.com/prometheus/node_exporter#textfile-collector). No network from the app. |

Use **separate** Prometheus textfile paths for the scoring worker and outbox dispatcher if both run on the same host so they do not overwrite each other (for example `/var/lib/node_exporter/textfile/coherence_scoring.prom` vs `coherence_outbox.prom`).

Threshold **warnings** in logs (and `warn` array in JSON) reuse existing env vars documented in `deploy/systemd/coherence-fund.env.example`.

## Operator quickstart: on-call provider env sets

These are minimal **exact env var sets** for in-process worker alert routing by deploy target. Keep `COHERENCE_FUND_OPS_ALERT_COOLDOWN_SECONDS` and `COHERENCE_FUND_OPS_ALERT_DEDUPE_KEY` at your defaults unless you need custom behavior.

### systemd (`deploy/systemd/coherence-fund.env`)

PagerDuty:

```bash
COHERENCE_FUND_OPS_ALERT_ROUTER_MODE=pagerduty
COHERENCE_FUND_OPS_ALERT_PAGERDUTY_ROUTING_KEY=<pagerduty_events_v2_routing_key>
# Optional (defaults to PagerDuty Events v2):
# COHERENCE_FUND_OPS_ALERT_PAGERDUTY_EVENTS_URL=https://events.pagerduty.com/v2/enqueue
```

Opsgenie:

```bash
COHERENCE_FUND_OPS_ALERT_ROUTER_MODE=opsgenie
COHERENCE_FUND_OPS_ALERT_OPSGENIE_API_KEY=<opsgenie_api_key>
# Optional (defaults to Opsgenie v2 alerts API):
# COHERENCE_FUND_OPS_ALERT_OPSGENIE_API_URL=https://api.opsgenie.com/v2/alerts
```

Alertmanager webhook receiver:

```bash
COHERENCE_FUND_OPS_ALERT_ROUTER_MODE=alertmanager
COHERENCE_FUND_OPS_ALERT_ALERTMANAGER_WEBHOOK_URL=https://alertmanager.example/api/v2/alerts
# Optional generic fallback path:
# COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL=https://alerts.example/hooks/coherence-fund
# COHERENCE_FUND_OPS_ALERT_WEBHOOK_TOKEN=<bearer_token_if_required>
```

### Kubernetes (`deploy/k8s/`)

ConfigMap (non-secret):

```yaml
data:
  COHERENCE_FUND_OPS_ALERT_ROUTER_MODE: "pagerduty"   # or opsgenie|alertmanager
```

Secret (provider credentials/endpoints):

```yaml
stringData:
  # PagerDuty
  COHERENCE_FUND_OPS_ALERT_PAGERDUTY_ROUTING_KEY: "<routing_key>"
  # Opsgenie
  COHERENCE_FUND_OPS_ALERT_OPSGENIE_API_KEY: "<api_key>"
  # Alertmanager
  COHERENCE_FUND_OPS_ALERT_ALERTMANAGER_WEBHOOK_URL: "https://alertmanager.example/api/v2/alerts"
```

### Helm (`deploy/helm/coherence-fund/`)

`values.yaml` / environment overlays:

```yaml
env:
  COHERENCE_FUND_OPS_ALERT_ROUTER_MODE: "pagerduty" # or opsgenie|alertmanager

secretEnv:
  # PagerDuty
  COHERENCE_FUND_OPS_ALERT_PAGERDUTY_ROUTING_KEY: "<routing_key>"
  # Opsgenie
  COHERENCE_FUND_OPS_ALERT_OPSGENIE_API_KEY: "<api_key>"
  # Alertmanager
  COHERENCE_FUND_OPS_ALERT_ALERTMANAGER_WEBHOOK_URL: "https://alertmanager.example/api/v2/alerts"
```

Quick verify after setting env:

```bash
python deploy/scripts/synthetic_alert_drill.py --verify-only --json
python deploy/scripts/synthetic_alert_drill.py --json
```

## SLO-oriented Prometheus metrics

Written to the textfile path when set. Names are stable for dashboards and alerts.

**Scoring worker** (`component` in JSON is `scoring`):

- `coherence_fund_scoring_eligible_queue_depth` — backlog eligible for claim.
- `coherence_fund_scoring_oldest_eligible_age_seconds` — age of oldest eligible job (0 if none).
- `coherence_fund_scoring_failed_dlq` — terminal failures.
- `coherence_fund_scoring_processing_in_flight` — leased, in-flight jobs.
- `coherence_fund_scoring_tick_processed` / `tick_failed` / `tick_idle` — last tick summary.

**Outbox dispatcher** (`component` is `outbox`):

- `coherence_fund_outbox_pending_dispatchable` — ready rows.
- `coherence_fund_outbox_oldest_pending_age_seconds` — oldest pending age (0 if none).
- `coherence_fund_outbox_failed_dlq` — terminal failures.
- `coherence_fund_outbox_tick_published` / `tick_failed` / `tick_scanned` — last tick summary.

**Warning flags** (1 = threshold fired for that component):

- `coherence_fund_worker_ops_warn_queue_depth{component="scoring|outbox"}`
- `coherence_fund_worker_ops_warn_oldest_latency{component="..."}`
- `coherence_fund_worker_ops_warn_failed_dlq{component="..."}`

## Grafana

Import `docs/ops/grafana/fund_worker_slo_dashboard.json` and point panels at your Prometheus datasource. Adjust thresholds to match your SLOs.

## SLO standards and runbooks

- `slo_threshold_standards.md` — baseline backlog, age, and DLQ targets; maps to env thresholds and alert tuning.
- `runbooks/production_observability_rollout.md` — phased rollout: sinks, dashboards, rules, Alertmanager routing, verification, **recurring CI drill cadence**, and GitHub Actions secret names.
- `runbooks/live_drill_staging.md` — staging **live** webhook drill execution and closure (links issue template + artifacts).
- `runbooks/live_drill_prod.md` — production **live** drill and acknowledgment closure (same hooks).

## Governed calibration dataset (historical outcomes)

Operator-exported JSON/JSONL batches can be folded into `data/governed/uncertainty_historical_outcomes.jsonl` **locally** (no network):

- **Shape:** see `deploy/ops/uncertainty-historical-outcomes-export.example.json` (governed field names plus an alias-key example). Same normalization rules as `calibrate-uncertainty`.
- **Pre-merge validation:** `python -m coherence_engine uncertainty-profile validate-historical-export --input path/to/export.jsonl` (exit **0** if all rows normalize, **2** otherwise). Add `--require-standard-layer-keys` to require all five canonical `layer_scores` keys. Deploy script: `deploy/scripts/validate_historical_outcomes_export.py`. The same strict check on the committed example runs in `.github/workflows/uncertainty-recalibration.yml`; locally use `make validate-historical-export-example`.
- **CLI:** `python -m coherence_engine uncertainty-profile merge-historical-dataset --dataset data/governed/uncertainty_historical_outcomes.jsonl --incoming path/to/export.jsonl --output data/governed/uncertainty_historical_outcomes.jsonl --manifest-out data/governed/uncertainty_historical_outcomes.manifest.json`  
  (Repeat `--incoming` for multiple files; set `PYTHONPATH` to the **parent directory of the repo folder** if `coherence_engine` is not installed — same rule as running tests from a checkout.)
- **Deploy script:** `python deploy/scripts/merge_governed_historical_outcomes.py` (same flags; adds the repo parent to `sys.path` automatically).
- **Semantics:** rows are normalized with the same rules as `calibrate-uncertainty`; duplicates are removed by a stable SHA-256 fingerprint of the governed record. With **no** `--incoming` arguments, the base file bytes are copied through unchanged (manifest checksum matches the existing committed dataset). When any incoming file is present, output lines are sorted by fingerprint (expect a new manifest checksum after adding rows).
- **Validation:** `python -m coherence_engine uncertainty-profile verify-dataset --dataset … --manifest …`

### Production → export extraction (scoring event export)

To build governed historical outcome rows from production scoring results, operators
join **CoherenceScored event payloads** (extracted from the outbox or dumped as JSON)
with an **outcomes annotation file** mapping `application_id → outcome_superiority`:

- **CLI:** `python -m coherence_engine uncertainty-profile export-historical-outcomes --scored-events events.json --outcomes outcomes.json --output export.json`  
  Add `--require-standard-layer-keys` to enforce all five layer keys. Use `--format jsonl` (or name `--output` with a `.jsonl` extension) for JSONL output.
- **Deploy script:** `python deploy/scripts/export_historical_outcomes.py --scored-events events.json --outcomes outcomes.json --output export.json` (adds repo parent to `sys.path` automatically).
- **Outcomes file shapes:** flat JSON object `{app_id: float}`, JSON array of `{application_id, outcome_superiority}`, or JSONL of the same.
- **Round-trip:** exported rows pass `validate-historical-export --require-standard-layer-keys` and can be merged with `merge-historical-dataset`.
- **Legacy events:** for CoherenceScored payloads without `n_contradictions`, the field is derived from `anti_gaming_score` and `n_propositions` (best-effort inverse).

## Governance / on-call ops hygiene review (local)

After downloading latest CI artifacts (or using saved copies), aggregate key JSON into one review bundle (no network):

```bash
python deploy/scripts/report_governance_ops_hygiene_review.py \
  --enrollment-json ./artifacts/governance-enrollment-coverage.json \
  --webhook-receipt-json ./artifacts/governance_escalation_webhook_delivery_receipt.json \
  --handoff-governance-json ./artifacts/oncall-tracker-handoff-governance.json \
  --routing-proof-json ./artifacts/governance_escalation_routing_proof.json \
  --json-out ./artifacts/governance_ops_hygiene_review.json \
  --markdown-out ./artifacts/governance_ops_hygiene_review.md
```

Omit any `--*-json` flag for sections you do not have; those blocks are marked skipped. Exit code is always **0** when the script runs (invalid JSON for a provided path is recorded under `input_errors`).

**Tests:** `pyproject.toml` sets `pythonpath = [".."]` under `[tool.pytest.ini_options]` so `pytest` resolves the `coherence_engine` package from a git checkout without an editable install. Ad-hoc `python -m coherence_engine …` still needs the repo parent on `PYTHONPATH` or a local install.

## Governance escalation + trend enrollment (CI)

- **Attestation lifecycle** (`.github/workflows/uncertainty-attestation-lifecycle.yml`) publishes **`governance-escalation-routing-proof`** (channel resolution) and **`governance-escalation-webhook-delivery-receipt`** (canonical canary/prod probe bytes, `canonical_payload_sha256`, optional `receipt_hmac_sha256` when secret **`GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY`** is set on environment **`uncertainty-governance-attestation-lifecycle`**). Scheduled runs **force dry-run** delivery mode; manual **Run workflow** can set input **`governance_webhook_verify_live_post`** to POST when webhook URLs resolve. Optional private map: secret **`GOVERNANCE_ESCALATION_ROUTING_MAP_JSON`** (otherwise the committed example map path is used). CLI: `python deploy/scripts/route_governance_attestation_escalation.py emit-webhook-delivery-receipt`.
- **Trend aggregation** (`.github/workflows/governance-attestation-trend-aggregation.yml`) can emit **`governance-enrollment-coverage.json`** / **`governance_enrollment_coverage.md`** via `deploy/scripts/report_governance_enrollment_coverage.py` when secret **`GOVERNANCE_ENROLLMENT_EXPECTED_REPO_LIST`** (newline-separated `owner/repo`) is set; optional SLA exception manifest body in **`GOVERNANCE_ENROLLMENT_SLA_EXCEPTION_MANIFEST_JSON`** (see `deploy/ops/governance-enrollment-sla-exception-manifest.example.json`). Repository variable **`GOVERNANCE_ENROLLMENT_FAIL_ON_MISSING=true`** turns non-compliance into a failing step (exit **2**).
- **Tracker handoff governance report** (`.github/workflows/oncall-tracker-handoff-governance-report.yml`, weekly) best-effort downloads the latest **`oncall-tracker-handoff`** artifact from **`oncall-route-verification.yml`** and publishes **`oncall-tracker-handoff-governance-report`** (`oncall_tracker_handoff_governance_summary`). No artifact → skip JSON, job still green.

## Recurring alert / route checks (CI)

The repository workflow `.github/workflows/oncall-route-verification.yml` exercises alert envelope construction and file-mode routing on a schedule **without required outbound calls** (safe verification always runs). It also runs `deploy/scripts/verify_oncall_route_policy.py` against `deploy/ops/oncall-route-policy.example.json` with **staleness** and **rotation** hardening flags (`--fail-on-stale-escalation-ownership`, `--require-escalation-rotation-ref`) and publishes artifacts (`oncall-release-readiness`: verification JSON, `oncall-policy-hardening-summary.md`, drill-evidence JSONL, release-readiness summary, **`oncall-incident-followup-checklist.md`**, plus env-specific **`oncall-live-drill-followup-staging.md`** / **`oncall-live-drill-followup-production.md`** and ticket templates **`oncall-ticket-payload-staging.json`** / **`oncall-ticket-payload-production.json`**). Optional repo variables **`ONCALL_TRACKER_STAGING_PROJECT`**, **`ONCALL_TRACKER_PRODUCTION_PROJECT`**, **`ONCALL_TRACKER_STAGING_LABELS`**, **`ONCALL_TRACKER_PRODUCTION_LABELS`** fill tracker routing in those generated files. Artifact **retention** defaults to 14 days per upload; set repository variable `ONCALL_VERIFICATION_ARTIFACT_RETENTION_DAYS` (1–90) to override.

**Live provider drill (opt-in):** manual **Run workflow** with `run_live_webhook_drill` and secret `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL`, and/or set repository variable `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL` to `true` so the same weekly schedule can POST a synthetic envelope (still skipped if the secret is unset). **Quiet windows (scheduled only):** optional repository variables `ONCALL_DRILL_QUIET_UTC_START` and `ONCALL_DRILL_QUIET_UTC_END` (each hour `0`–`23` UTC); when both are set, scheduled live POSTs are suppressed for hours inside that range (wraps past midnight if start > end). Manual dispatch ignores quiet windows. Details and secret names: `runbooks/production_observability_rollout.md`. After a live drill, use **`oncall-incident-followup-checklist.md`** and the generated **`oncall-live-drill-followup-*.md`** / **`oncall-ticket-payload-*.json`** files from the artifact bundle; file **On-call live drill follow-up** (`.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`) or your external tracker. **Tracker API handoff (separate opt-in):** input `post_tracker_handoff` and/or variable `ONCALL_POST_TRACKER_HANDOFF=true` runs job **`optional-tracker-handoff`**, which runs **`deploy/scripts/validate_oncall_tracker_handoff.py`** to enforce **per-provider ticket contracts** (required fields and types for **`generic`** / **`jira`** / **`github`**) before any live POST when a URL secret is set, then POSTs to **`ONCALL_TRACKER_*_HANDOFF_URL`** (optional **`*_HANDOFF_TOKEN`**). Repository variables **`ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER`** / **`ONCALL_TRACKER_PRODUCTION_HANDOFF_PROVIDER`** select the adapter: **`generic`** (default: raw ticket JSON body), **`jira`** (Jira Cloud REST v3 issue create JSON from the template), or **`github`** (GitHub Issues API JSON). Optional **retry/idempotency policy overlay**: repository secret **`ONCALL_TRACKER_HANDOFF_POLICY_JSON`** (full JSON; **wins** over the path variable) and/or variable **`ONCALL_TRACKER_HANDOFF_POLICY_PATH`** (repo-relative path); see **`deploy/ops/oncall_tracker_handoff_policy.example.json`** for `defaults` and per-environment keys (`max_attempts`, `retryable_http_statuses`, `backoff_*`, `idempotency_mode` `run_env_payload` or `off`). The script records **`governance_audit.policy_resolution`** (which input supplied the overlay), **`policy_drift_vs_builtin_defaults`** (diff vs clamped built-ins for drift monitoring), and per-row **`closure_artifacts`** (links to follow-up markdown + evidence filenames in **`oncall-release-readiness`**) plus **`response.reconciliation`** (issue key / number / redacted URL host+path only—no raw response bodies or tokens). Repository variable **`ONCALL_TRACKER_HANDOFF_WRITEBACK_RECONCILIATION=true`** runs **`writeback-reconciliation`** so follow-up **`oncall-live-drill-followup-*.md`** files in the **`oncall-tracker-handoff`** bundle get an appended **`## Tracker reconciliation (automation write-back)`** section (idempotent). Defaults match the previous behavior (four attempts, selected **408**/**429**/5xx, capped bounds in the script). **`Idempotency-Key`** is omitted when `idempotency_mode` is **`off`**. The safe job runs **`ci-check`** on generated ticket templates (prints the same **`policy_resolution`** line and **`policy_drift_vs_builtin_defaults`** per env) plus **`tests/test_validate_oncall_tracker_handoff.py`**. Uploads **`oncall-tracker-handoff`** / **`oncall-tracker-handoff-results.json`** (schema **`oncall_tracker_handoff_results/v2`**). Jira Cloud often needs **`Authorization: Basic …`** in the token secret (paste the full `Basic base64(email:api_token)` value, or a literal `Bearer …` if your gateway expects it). Env-specific steps: `runbooks/live_drill_staging.md`, `runbooks/live_drill_prod.md`. Uncertainty profile shadow promotion is gated in `.github/workflows/uncertainty-recalibration.yml`.

## On-call policy file (per environment)

Use a JSON registry (see `deploy/ops/oncall-route-policy.example.json`) to document **environment → secret-manager provider → on-call provider → receiver / escalation** mapping. Validate locally with `verify_oncall_route_policy.py` (no provider APIs). Use `--env NAME --check-env` to compare the policy row to `COHERENCE_FUND_SECRET_MANAGER_PROVIDER` and `COHERENCE_FUND_OPS_ALERT_ROUTER_MODE` in the current process environment.

**Ownership freshness (staleness):** set root `escalation_ownership_reviewed_at` (ISO date) when you last confirmed escalation policies and receivers match reality. The verifier defaults `--max-escalation-ownership-age-days` to **90**; override per file with `escalation_ownership_max_age_days` (the repository example uses a longer window so the committed template does not fail CI every quarter). Scheduled workflow runs pass `--fail-on-stale-escalation-ownership` so missing or expired review dates fail the job.

**Rotation documentation:** for `pagerduty` and `opsgenie` rows, set `escalation_rotation_ref` to a stable label for the primary schedule or rotation (schedule name, ID, or internal CMDB key). Scheduled CI passes `--require-escalation-rotation-ref` so those rows cannot ship empty. For deterministic tests, use `--reference-time ISO8601` (UTC).

## Alert routing (Alertmanager)

Prometheus alerts are labeled for routing (`severity`, `team`, `service`, and `component` where applicable).

1. Match `service="coherence-fund"` (and optionally `team`) in Alertmanager `route`/`routes` to select Slack, email, PagerDuty, Opsgenie, etc.
2. Prefer one delivery path for worker SLO rules (either raw `PrometheusRule` or Helm-managed rules) to avoid duplicates.
3. Optional in-process alert routing can be enabled with `COHERENCE_FUND_OPS_ALERT_*` env vars (file or webhook mode); these are consumed by fund worker services and remain disabled by default.

## Rule assets

- Kubernetes (Prometheus Operator): `deploy/k8s/alerts/fund-worker-slo-rules.yaml` — apply manually; not part of the default kustomization.
- Helm: set `prometheusRules.enabled: true` in chart values; template `templates/prometheus-rules.yaml` is gated and omitted when disabled.
