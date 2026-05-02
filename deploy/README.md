# Deployment Bundle

Deployment assets are organized by platform:

- `deploy/systemd/` for VM/bare-metal Linux deployments
- `deploy/k8s/` for Kubernetes deployments
- `deploy/helm/coherence-fund/` for Helm-based Kubernetes deployments

Production overrides:

- `deploy/k8s/overlays/prod/`
- `deploy/k8s/overlays/prod-redis/`
- `deploy/k8s/overlays/prod-sqs/`
- `deploy/helm/coherence-fund/values-prod.yaml` (+ broker overlays)

Both bundles include:

- API runtime definition
- Outbox dispatcher worker runtime definition
- Scoring queue worker runtime definition
- environment templates
- health/readiness guidance

Worker SLO telemetry: optional JSONL and Prometheus textfile sinks (env-driven, no default network); see `docs/ops/README.md` and the Grafana import under `docs/ops/grafana/`.

Observability rollout, SLO baselines, and alerts:

- `docs/ops/runbooks/production_observability_rollout.md`
- `docs/ops/slo_threshold_standards.md`
- `deploy/k8s/alerts/fund-worker-slo-rules.yaml` (optional `PrometheusRule`; apply separately from default kustomize)
- Helm: `prometheusRules.enabled` in `deploy/helm/coherence-fund/values.yaml` gates `templates/prometheus-rules.yaml`

Security/rotation operations:

- `docs/SECRET_MANAGER_ROTATION_RUNBOOK.md` for strict provider policy and key rotation procedures.
- `python deploy/scripts/secret_manager_preflight.py --require-reachable` for fail-fast deploy checks.
- `make preflight-secret-manager` as the required pre-deploy gate across systemd/k8s/helm checklists.
- `.github/workflows/deploy-preflight-gate.yml` provides a dedicated CI gate that blocks release unless preflight passes for the selected provider profile.
- `.github/workflows/release.yml` wires production deploy behind the reusable preflight gate so deploy jobs cannot execute unless preflight succeeds.
- `.github/workflows/secret-manager-staging-integration.yml` runs cloud-backed staging integration tests for provider-policy + reachability (manual, staging-gated).
- `.github/workflows/nonprod-release-smoke.yml` runs non-prod smoke checks for helm/k8s/systemd release paths.
- `.github/workflows/oncall-route-verification.yml` runs weekly (safe mode: on-call **policy JSON** verification via `deploy/scripts/verify_oncall_route_policy.py`, YAML checks, alert-routing tests, policy verifier tests, synthetic file drill; no required outbound network). Uploads **`oncall-release-readiness`** artifacts (verification JSON, drill-evidence JSONL, policy hardening summary, release-readiness summary, **`oncall-incident-followup-checklist.md`**). Optional live webhook drill: manual **Run workflow** (`run_live_webhook_drill`) and/or repository variable `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL=true` on schedule, with secret `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL` (see `docs/ops/runbooks/production_observability_rollout.md` for optional UTC quiet-window vars and artifact retention `ONCALL_VERIFICATION_ARTIFACT_RETENTION_DAYS`); optional artifact **`oncall-live-webhook-drill`**. Optional **tracker handoff**: `post_tracker_handoff` and/or `ONCALL_POST_TRACKER_HANDOFF=true` with secrets `ONCALL_TRACKER_STAGING_HANDOFF_URL` / `ONCALL_TRACKER_PRODUCTION_HANDOFF_URL` (optional `*_HANDOFF_TOKEN`); artifact **`oncall-tracker-handoff`**. Live drill closure runbooks: `docs/ops/runbooks/live_drill_staging.md`, `docs/ops/runbooks/live_drill_prod.md`; post-drill GitHub issue template: `.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`. Post-drill closure: `docs/ops/runbooks/live_drill_staging.md`, `docs/ops/runbooks/live_drill_prod.md`, and GitHub issue template `.github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml`.
- `deploy/ops/oncall-route-policy.example.json` — template for environment → on-call provider → receiver/escalation mapping (copy privately; validate with `verify_oncall_route_policy.py`).
- `.github/workflows/uncertainty-recalibration.yml` schedules weekly calibration; manual **promote to shadow** requires `governance_acknowledged=true` and matching `UNCERTAINTY_SHADOW_PROMOTION_TOKEN` / `promotion_approval_token` (see workflow inputs).

Release workflow required secrets by target:

- Kubernetes/Helm: `KUBECONFIG_B64`
- systemd over SSH: `SYSTEMD_SSH_PRIVATE_KEY` (optional `SYSTEMD_SSH_KNOWN_HOSTS`)
- Vault profile: `VAULT_TOKEN`

On-call / governance (optional, repository Actions secrets):

- `ONCALL_ROUTE_VERIFICATION_WEBHOOK_URL` — optional; enables live webhook drill in `oncall-route-verification.yml` (manual and/or scheduled when `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL=true`)
- `ONCALL_ROUTE_VERIFICATION_WEBHOOK_TOKEN` — optional Bearer token for the same drill
- `ONCALL_TRACKER_STAGING_HANDOFF_URL` / `ONCALL_TRACKER_PRODUCTION_HANDOFF_URL` — optional; POST targets for tracker handoff when `post_tracker_handoff` or `ONCALL_POST_TRACKER_HANDOFF=true` enables `optional-tracker-handoff` (body shape depends on provider variable below)
- `ONCALL_TRACKER_STAGING_HANDOFF_TOKEN` / `ONCALL_TRACKER_PRODUCTION_HANDOFF_TOKEN` — optional auth for those POSTs (`Bearer …` by default; secrets may instead contain a full `Basic …` or `Bearer …` header value for Jira/GitHub)
- Repository variables (optional): `ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL`, `ONCALL_DRILL_QUIET_UTC_START` / `ONCALL_DRILL_QUIET_UTC_END`, `ONCALL_VERIFICATION_ARTIFACT_RETENTION_DAYS`, `ONCALL_POST_TRACKER_HANDOFF`, `ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER`, `ONCALL_TRACKER_PRODUCTION_HANDOFF_PROVIDER` (`generic` | `jira` | `github`; default generic) — see `docs/ops/runbooks/production_observability_rollout.md`
- `UNCERTAINTY_SHADOW_PROMOTION_TOKEN` — required when using **Promote to shadow** on `uncertainty-recalibration.yml` (paired with workflow input `promotion_approval_token`)

Per-environment runtime keys for secret managers and worker ops alerts are listed in `deploy/systemd/coherence-fund.env.example`, `deploy/k8s/base/configmap-env-template.yaml`, and Helm `values.yaml` / `values-prod.yaml`.
