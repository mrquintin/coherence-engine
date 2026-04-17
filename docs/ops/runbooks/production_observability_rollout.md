# Runbook: production observability rollout (fund workers)

Use this runbook when enabling worker SLO telemetry, dashboards, and alerts in a new production (or production-like) environment.

## Preconditions

- Fund API and workers deployed (systemd, raw Kubernetes, or Helm).
- PostgreSQL and the selected outbox backend (Kafka, SQS, or Redis) healthy.
- `make preflight-secret-manager` passes (see `deploy/README.md`).
- Prometheus (or compatible scraper) can reach metrics: typically node_exporter textfile paths on VMs, or your cluster’s metrics pipeline for sidecar/scrape configs.

## Rollout phases

### 1. Enable telemetry sinks (no alert noise)

1. **Logs**: confirm `COHERENCE_FUND_WORKER_OPS_SNAPSHOT` appears in worker logs on a steady interval.
2. **Optional JSONL** (audit/debug): set `COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH` to a rotated path with appropriate permissions.
3. **Optional Prometheus textfile**: set `COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH` **per worker role** so files are not overwritten (for example separate filenames for outbox vs scoring on the same host).

Keep in-process threshold env vars at **0** until Prometheus scraping and dashboards are verified (see `docs/ops/slo_threshold_standards.md`).

### 2. Dashboards

1. Import `docs/ops/grafana/fund_worker_slo_dashboard.json`.
2. Point the datasource at the Prometheus instance that scrapes the worker metrics.
3. Validate panels show non-empty series under normal load. Adjust panel thresholds to match your adopted SLO table.

### 3. Alert rules

Choose **one** primary delivery path to avoid duplicate pages:

- **Kubernetes + Prometheus Operator**: apply `deploy/k8s/alerts/fund-worker-slo-rules.yaml` (edit `namespace` and labels first), **or**
- **Helm**: set `prometheusRules.enabled: true` and tune `prometheusRules.alertTeam`, `alertSeverity`, and `extraAlertLabels` in your values file (see `deploy/helm/coherence-fund/README.md`).

Silence or mute rules during intentional maintenance windows.

### 4. Alert routing (Alertmanager and on-call providers)

Alerts are only useful if they reach the right receiver with context.

1. **Standardize labels** on rules: `severity`, `team`, `service` (Helm defaults provide a baseline; raw YAML may need edits).
2. **Alertmanager** `route` tree: match on `service="coherence-fund"` and `team` to select Slack, email, PagerDuty, or Opsgenie receivers.
3. **Inhibition**: optionally suppress `warning` backlog alerts when `critical` DLQ alerts fire for the same component.
4. **Runbook links**: set `runbook_url` in your overlay to this file or your internal wiki so responders land on the right remediation steps.

#### Environment → provider → receiver / escalation registry

Maintain a **single policy document** per org (not committed with real secrets) that lists each environment’s:

| Field | PagerDuty | Opsgenie | Alertmanager |
|--------|-----------|----------|----------------|
| Integration target | Service or Events v2 URL + **escalation policy** ID/name | Team + **escalation policy** | **Receiver** name in `receivers:` |
| Typical label match | N/A (payload routing) | N/A | `service`, `team`, `environment` in `route` matchers |
| In-process webhook (`COHERENCE_FUND_OPS_ALERT_*`) | Generic/v2 webhook → PD | Opsgenie API / webhook integration | `webhook_configs` pointing at AM or bridge |

**Document-level metadata (recommended for governance):**

| Field | Purpose |
|-------|---------|
| `escalation_ownership_reviewed_at` | ISO date (or datetime) when someone last verified escalation policies, receivers, and on-call rotations against the live provider. |
| `escalation_ownership_max_age_days` | Optional override for the maximum allowed age of `escalation_ownership_reviewed_at` (default verifier default is **90** days when not set on the file). |

**Per-environment (PagerDuty / Opsgenie):**

| Field | Purpose |
|-------|---------|
| `escalation_rotation_ref` | Stable reference to the primary on-call **schedule or rotation** (name, ID, or CMDB key). Required in scheduled CI when using `--require-escalation-rotation-ref`. |

- **Template**: `deploy/ops/oncall-route-policy.example.json` — copy to a private path (for example `deploy/ops/oncall-route-policy.json` in a private overlay or gitignored file).
- **Verification** (deterministic, **no outbound calls**): `python deploy/scripts/verify_oncall_route_policy.py --policy /path/to/policy.json`
- **Scheduled / strict checks** (same flags as `.github/workflows/oncall-route-verification.yml`): add `--fail-on-stale-escalation-ownership` and `--require-escalation-rotation-ref` so stale review dates and missing rotation refs fail CI. Tune age with `--max-escalation-ownership-age-days` or per-file `escalation_ownership_max_age_days`.
- **Optional env cross-check** (still local): set `COHERENCE_FUND_SECRET_MANAGER_PROVIDER` and `COHERENCE_FUND_OPS_ALERT_ROUTER_MODE` as on the host/Pod, then:
  `python deploy/scripts/verify_oncall_route_policy.py --policy ... --env prod --check-env`

PagerDuty and Opsgenie rows **must** include non-empty `escalation_policy_ref`. Alertmanager rows **must** include `prometheus_alert_route_labels` (object) so your `route` matchers are unambiguous.

### 5. Raise in-process warnings (optional)

After Prometheus alerts are trusted, set non-zero `COHERENCE_FUND_*_OPS_*_WARN_*` values aligned with `docs/ops/slo_threshold_standards.md` so logs and JSONL gain explicit `warn` tags without changing application code.

## Verification checklist

- [ ] Textfile or scrape target shows `coherence_fund_scoring_*` and `coherence_fund_outbox_*` gauges.
- [ ] Grafana dashboard panels populate.
- [ ] Test alert fires in a non-production environment (use a temporary low threshold or `amtool` silence verification).
- [ ] On-call receiver acknowledges a synthetic page.
- [ ] Runbook URL or internal doc linked from alert annotations.

## Recurring verification cadence (CI + repository)

| Cadence | Mechanism | Network | Purpose |
|---------|-----------|---------|---------|
| Weekly (Wed 14:00 UTC, adjustable) | `.github/workflows/oncall-route-verification.yml` schedule | **None required** (safe job) | `verify_oncall_route_policy.py` on repo example policy (**staleness** + **rotation** flags); parse deploy YAML templates; `tests/test_ops_alert_routing.py` + `tests/test_oncall_route_policy_verifier.py`; **synthetic** file-mode `coherence_fund_worker_ops_alert/v1` drill. Uploads artifact **`oncall-release-readiness`** (`oncall-route-verification.json`, `oncall-policy-hardening-summary.md`, `oncall-drill-evidence.jsonl`, `release-readiness-summary.md`, **`oncall-incident-followup-checklist.md`**, env-specific **`oncall-live-drill-followup-staging.md`** / **`oncall-live-drill-followup-production.md`**, **`oncall-ticket-payload-staging.json`** / **`oncall-ticket-payload-production.json`**). |
| Opt-in (any trigger) | Same workflow; job `optional-tracker-handoff` | Optional HTTPS | Enable with **`post_tracker_handoff`** on **Run workflow** and/or repository variable **`ONCALL_POST_TRACKER_HANDOFF=true`**. Checks out the repo and runs **`deploy/scripts/validate_oncall_tracker_handoff.py run`**, which downloads **`oncall-release-readiness`** artifacts and POSTs per environment when the matching URL secret is set; missing URL → skip (no job failure). **Contracts**: invalid ticket payloads for the selected provider **fail the job** (exit **2**) when a URL is configured—no blind POST. **Adapter** is selected with **`ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER`** / **`ONCALL_TRACKER_PRODUCTION_HANDOFF_PROVIDER`**: **`generic`**, **`jira`**, or **`github`**. Optional policy overlay: secret **`ONCALL_TRACKER_HANDOFF_POLICY_JSON`** (overrides variable) and/or **`ONCALL_TRACKER_HANDOFF_POLICY_PATH`** (see **`deploy/ops/oncall_tracker_handoff_policy.example.json`**) for per-environment **`max_attempts`**, **`retryable_http_statuses`**, backoff caps, and **`idempotency_mode`** (`run_env_payload` or **`off`**). **`governance_audit`** records **`policy_resolution`** (precedence + which inputs were set), **`policy_drift_vs_builtin_defaults`** vs **`builtin_defaults_reference`**, and per-row **`closure_artifacts`** (follow-up markdown + evidence refs) plus **`response.reconciliation`** (issue key / redacted URL hints on success—no raw bodies or tokens). Optional repository variable **`ONCALL_TRACKER_HANDOFF_WRITEBACK_RECONCILIATION=true`** runs **`writeback-reconciliation`** so **`oncall-live-drill-followup-*.md`** copies bundled in **`oncall-tracker-handoff`** gain an appended reconciliation section (idempotent). Default **`Idempotency-Key`**: SHA-256(`run_id|environment|sha256(ticket file)`). Uploads **`oncall-tracker-handoff`** / **`oncall-tracker-handoff-results.json`** (**`oncall_tracker_handoff_results/v2`**). |
| Weekly (Mon 09:40 UTC) | `.github/workflows/oncall-tracker-handoff-governance-report.yml` | GitHub API (read) | Best-effort: downloads latest **`oncall-tracker-handoff`** from a successful **`oncall-route-verification.yml`** run and publishes **`oncall-tracker-handoff-governance-report`** (`oncall_tracker_handoff_governance_summary` JSON + markdown). Missing artifact → skip JSON, job succeeds. |
| Same as attestation lifecycle schedule | `.github/workflows/uncertainty-attestation-lifecycle.yml` job **`governance-escalation-webhook-delivery-receipt`** | Optional HTTPS (manual only) | Deterministic canary/prod probe receipts: **`governance-escalation-webhook-delivery-receipt`** artifact (`governance_escalation_webhook_delivery_receipt`). Scheduled runs force **dry-run**; manual dispatch can set **`governance_webhook_verify_live_post`** for live POST when URLs resolve. Optional HMAC signing: secret **`GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY`** on environment **`uncertainty-governance-attestation-lifecycle`**. |
| Same as trend aggregation schedule | `.github/workflows/governance-attestation-trend-aggregation.yml` | None (local) | When secret **`GOVERNANCE_ENROLLMENT_EXPECTED_REPO_LIST`** is set, emits **`governance-enrollment-coverage.json`** / **`governance_enrollment_coverage.md`** vs the trend aggregate; optional **`GOVERNANCE_ENROLLMENT_SLA_EXCEPTION_MANIFEST_JSON`** and variable **`GOVERNANCE_ENROLLMENT_FAIL_ON_MISSING`**. |
| On demand (local) | `deploy/scripts/report_governance_ops_hygiene_review.py` | None | Paste paths to downloaded **`governance-enrollment-coverage`**, **`governance-escalation-webhook-delivery-receipt`**, **`oncall-tracker-handoff-governance`**, and **`governance-escalation-routing-proof`** JSON; emits **`governance_ops_hygiene_review`** JSON + Markdown with reminders (secret rotation, drift, enrollment gaps). See `docs/ops/README.md`. |
| On demand | Same workflow **Run workflow** | Optional HTTPS | Set `run_live_webhook_drill` to POST a synthetic envelope when `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL` is set. Manual runs **ignore** scheduled quiet-window variables. Successful POST uploads **`oncall-live-webhook-drill`**. |
| Same weekly schedule (opt-in) | Same workflow; repository variable `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL=true` | Optional HTTPS | Reuses the weekly cron: live POST runs only when the variable is exactly `true`, the webhook secret is set, and the current UTC hour is **outside** any configured quiet window (`ONCALL_DRILL_QUIET_UTC_START` / `ONCALL_DRILL_QUIET_UTC_END`, both `0`–`23`; omit either var to disable quiet suppression). |
| Weekly (Mon 06:00 UTC) | `.github/workflows/uncertainty-recalibration.yml` | None for calibration | Governed dataset verify + calibrate; **shadow promotion** is manual and gated (see below). |

### GitHub Actions repository variables (optional, on-call workflow)

| Variable | Used by | Notes |
|--------|---------|--------|
| `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL` | `oncall-route-verification.yml` | Set to `true` to allow **scheduled** live webhook POST on the weekly cron (in addition to manual dispatch). Default unset = scheduled runs stay non-network for the live job. |
| `ONCALL_DRILL_QUIET_UTC_START` | same | Hour `0`–`23` UTC; use with `ONCALL_DRILL_QUIET_UTC_END` to define a quiet range for **scheduled** live drills only (e.g. `22` and `6` suppresses 22:00–05:59 UTC). |
| `ONCALL_DRILL_QUIET_UTC_END` | same | End hour (exclusive upper bound in the same semantics as the workflow gate). |
| `ONCALL_VERIFICATION_ARTIFACT_RETENTION_DAYS` | same | Integer `1`–`90`; retention for `oncall-release-readiness` and `oncall-live-webhook-drill` uploads (default `14` when unset/invalid). |
| `ONCALL_TRACKER_STAGING_PROJECT` | same | Optional. Staging tracker project / board key embedded in **`oncall-live-drill-followup-staging.md`** and **`oncall-ticket-payload-staging.json`**. |
| `ONCALL_TRACKER_PRODUCTION_PROJECT` | same | Optional. Production tracker project / board key in **`oncall-live-drill-followup-production.md`** and **`oncall-ticket-payload-production.json`**. |
| `ONCALL_TRACKER_STAGING_LABELS` | same | Optional. Comma-separated labels for staging outputs (defaults to `oncall-drill,staging` when unset). |
| `ONCALL_TRACKER_PRODUCTION_LABELS` | same | Optional. Comma-separated labels for production outputs (defaults to `oncall-drill,production` when unset). |
| `ONCALL_POST_TRACKER_HANDOFF` | same | Set to `true` to run **`optional-tracker-handoff`** on **every** workflow run (including schedule) after the safe job succeeds—still **no outbound calls** unless the matching `ONCALL_TRACKER_*_HANDOFF_URL` secrets are set (each env skips when its URL secret is absent). Prefer leaving unset and using manual **Run workflow** + input **`post_tracker_handoff`** for one-off handoffs. |
| `ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER` | same | Optional. Tracker handoff **adapter** for staging: `generic` (default), `jira`, or `github`. See secrets table for URL expectations. |
| `ONCALL_TRACKER_PRODUCTION_HANDOFF_PROVIDER` | same | Optional. Same as staging, for the production payload row. |
| `ONCALL_TRACKER_HANDOFF_POLICY_PATH` | same | Optional. Repo-relative path to JSON policy overlay (`defaults` + `environments.staging` / `environments.production`) for retry/idempotency bounds. Ignored if secret **`ONCALL_TRACKER_HANDOFF_POLICY_JSON`** is non-empty. |
| `ONCALL_TRACKER_HANDOFF_WRITEBACK_RECONCILIATION` | same | Set to `true` to append **`## Tracker reconciliation (automation write-back)`** into **`oncall-live-drill-followup-*.md`** after tracker handoff ( **`optional-tracker-handoff`** job only). |

### GitHub Actions secrets (optional)

| Secret | Used by | Notes |
|--------|---------|--------|
| `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL` | `oncall-route-verification.yml` | Alertmanager, PagerDuty Events v2 generic, Opsgenie, Slack incoming webhook, or any HTTPS endpoint accepting JSON POSTs of `coherence_fund_worker_ops_alert/v1`. |
| `ONCALL_ROUTE_VERIFICATION_WEBHOOK_TOKEN` | same | Optional `Authorization: Bearer …` header. |
| `ONCALL_TRACKER_STAGING_HANDOFF_URL` | `oncall-route-verification.yml` | Optional. HTTPS **POST** target for staging handoff when **`post_tracker_handoff`** and/or **`ONCALL_POST_TRACKER_HANDOFF=true`** enables `optional-tracker-handoff`. **`generic`**: body is raw **`oncall-ticket-payload-staging.json`**. **`jira`**: use your Jira Cloud issue-create URL (e.g. `…/rest/api/3/issue`); **`tracker_project_key`** in the ticket JSON must be the Jira project key. **`github`**: use `https://api.github.com/repos/OWNER/REPO/issues` (PAT in token secret). |
| `ONCALL_TRACKER_STAGING_HANDOFF_TOKEN` | same | Optional auth for staging handoff. Default formatting: `Authorization: Bearer <secret>` unless the secret already starts with `Basic ` or `Bearer `. Never written to result artifacts. |
| `ONCALL_TRACKER_PRODUCTION_HANDOFF_URL` | same | Same as staging, for **`oncall-ticket-payload-production.json`** / production adapter behavior. |
| `ONCALL_TRACKER_PRODUCTION_HANDOFF_TOKEN` | same | Optional auth for production handoff (same rules as staging). |
| `ONCALL_TRACKER_HANDOFF_POLICY_JSON` | `oncall-route-verification.yml` | Optional. Full JSON policy document for tracker handoff (same shape as **`deploy/ops/oncall_tracker_handoff_policy.example.json`**). When set and non-empty, overrides **`ONCALL_TRACKER_HANDOFF_POLICY_PATH`**. Never echoed in artifacts. |
| `UNCERTAINTY_SHADOW_PROMOTION_TOKEN` | `uncertainty-recalibration.yml` | Required when **Promote to shadow** is enabled; must match workflow input `promotion_approval_token`, and `governance_acknowledged` must be `true`. |

### Incident follow-up after a live drill

Use the CI artifact **`oncall-incident-followup-checklist.md`** (inside `oncall-release-readiness`) for acknowledgment, noise review, policy/registry updates, and linking tickets. The same bundle includes **environment-specific** follow-up bodies (**`oncall-live-drill-followup-staging.md`**, **`oncall-live-drill-followup-production.md`**) and ticket JSON templates (**`oncall-ticket-payload-staging.json`**, **`oncall-ticket-payload-production.json`**); optional repository variables **`ONCALL_TRACKER_*_PROJECT`** and **`ONCALL_TRACKER_*_LABELS`** tune tracker routing. The workflow run summary links the checklist and these templates when a live POST completes.

**Direct tracker handoff (optional):** enable **`post_tracker_handoff`** on **Run workflow** and/or set **`ONCALL_POST_TRACKER_HANDOFF=true`**, then configure **`ONCALL_TRACKER_STAGING_HANDOFF_URL`** / **`ONCALL_TRACKER_PRODUCTION_HANDOFF_URL`** (and optional **`*_HANDOFF_TOKEN`** secrets). Set **`ONCALL_TRACKER_*_HANDOFF_PROVIDER`** when you need **`jira`** or **`github`** JSON instead of the default **`generic`** raw ticket file POST. **`validate_oncall_tracker_handoff.py`** validates ticket contracts before POST; invalid payloads fail the job when a URL is set. Optional **`ONCALL_TRACKER_HANDOFF_POLICY_JSON`** (wins) / **`ONCALL_TRACKER_HANDOFF_POLICY_PATH`** tune retries and **`Idempotency-Key`** behavior per environment (see **`deploy/ops/oncall_tracker_handoff_policy.example.json`**); results include **`governance_audit.policy_resolution`**, **`policy_drift_vs_builtin_defaults`**, **`closure_artifacts`** (links into the **`oncall-release-readiness`** bundle), and **`response.reconciliation`** for delivery closure. Outcomes are in artifact **`oncall-tracker-handoff`** (`oncall-tracker-handoff-results.json`, schema **`oncall_tracker_handoff_results/v2`**) with redacted request URL metadata only (no tokens).

**Environment runbooks** (execution + operational closure):

- **Staging / non-prod**: `docs/ops/runbooks/live_drill_staging.md` — webhook targets staging receivers; keep scheduled `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL` off if you only want manual drills.
- **Production**: `docs/ops/runbooks/live_drill_prod.md` — production webhook and acknowledgment SLO; prefer manual `workflow_dispatch` for timing control.

**Ticket automation (GitHub):** after a live drill, open **Issues → New issue → On-call live drill follow-up** (template file `.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`). Paste the Actions run URL and mirror critical rows from the artifact checklist for an auditable record.

**Environment-specific closure:** follow `runbooks/live_drill_staging.md` for non-prod live drills and `runbooks/live_drill_prod.md` for production (or production-equivalent) drills—preconditions, execution, and same-shift closure.

**Tracked follow-up in GitHub:** create **Issues → New issue → On-call live drill follow-up** (`.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`) and paste the Actions run URL plus delivery/ack details. This mirrors the artifact checklist for audit and links evidence files (`oncall-drill-evidence.jsonl`, `oncall-live-webhook-drill.json` when present).

### Runtime environment keys (per deployment target)

Align **secret manager** and **ops alert** variables with your environment; templates are split by surface:

| Concern | systemd | Kubernetes ConfigMap | Helm values |
|---------|---------|----------------------|-------------|
| Provider choice | `COHERENCE_FUND_SECRET_MANAGER_PROVIDER` + `COHERENCE_FUND_AWS_REGION` / GCP token / Vault addr | Same keys in `deploy/k8s/configmap-env-template.yaml` | `env` + `secretEnv` in `deploy/helm/coherence-fund/values*.yaml` |
| AWS | `COHERENCE_FUND_AWS_REGION` | same | same |
| GCP | `COHERENCE_FUND_GCP_ACCESS_TOKEN` (automation only; prefer WIF on host) | inject via Secret for Jobs if needed | workload identity on service account |
| Vault | `COHERENCE_FUND_VAULT_ADDR`, `COHERENCE_FUND_VAULT_TOKEN` or `_TOKEN_FILE` | `secret-template.yaml` | `secretEnv` |
| In-process worker ops alerts | `COHERENCE_FUND_OPS_ALERT_ROUTER_MODE`, `COHERENCE_FUND_OPS_ALERT_FILE_PATH` or `COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL` / `_TOKEN` | Prefer **Secret** for webhook URL/token | Use Kubernetes Secret references for webhooks in production |

See `deploy/systemd/coherence-fund.env.example`, `deploy/k8s/configmap-env-template.yaml`, and `deploy/README.md` for the full lists.

## Rollback

- Disable `prometheusRules.enabled` or remove the `PrometheusRule` apply.
- Unset textfile paths and restart workers.
- Set all `*_OPS_*_WARN_*` env vars to `0` or unset.

## Related documents

- `docs/ops/slo_threshold_standards.md`
- `docs/ops/README.md`
- `deploy/README.md`
- `docs/ops/runbooks/live_drill_staging.md`
- `docs/ops/runbooks/live_drill_prod.md`
