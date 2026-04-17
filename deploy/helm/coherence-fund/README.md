# Coherence Fund Helm Chart

## Install

Required preflight before install/upgrade:

```bash
make preflight-secret-manager
```

```bash
helm upgrade --install coherence-fund deploy/helm/coherence-fund -n coherence-fund --create-namespace
```

Production install:

```bash
helm upgrade --install coherence-fund deploy/helm/coherence-fund \
  -n coherence-fund --create-namespace \
  -f deploy/helm/coherence-fund/values-prod.yaml
```

Broker-specific production tuning:

```bash
# Kafka
helm upgrade --install coherence-fund deploy/helm/coherence-fund -n coherence-fund -f deploy/helm/coherence-fund/values-prod.yaml -f deploy/helm/coherence-fund/values-prod-kafka.yaml

# Redis
helm upgrade --install coherence-fund deploy/helm/coherence-fund -n coherence-fund -f deploy/helm/coherence-fund/values-prod.yaml -f deploy/helm/coherence-fund/values-prod-redis.yaml

# SQS
helm upgrade --install coherence-fund deploy/helm/coherence-fund -n coherence-fund -f deploy/helm/coherence-fund/values-prod.yaml -f deploy/helm/coherence-fund/values-prod-sqs.yaml
```

## Configure

Edit `values.yaml` for:

- image repository/tag
- DB and broker secrets
- outbox backend (`kafka|sqs|redis`)
- autoscaling/PDB/network policy toggles

## Includes

- API Deployment + Service
- Outbox worker Deployment
- Scoring worker Deployment
- Migration pre-install/pre-upgrade Job
- HPA (API + outbox worker + scoring worker)
- PodDisruptionBudget (API + outbox worker + scoring worker)
- NetworkPolicy set (default deny, API ingress, controlled egress)
- Optional `PrometheusRule` (`templates/prometheus-rules.yaml`) when `prometheusRules.enabled` is `true` (Prometheus Operator / `monitoring.coreos.com/v1`)

## PrometheusRule (optional)

Worker SLO alerts are **off** by default (`prometheusRules.enabled: false`). When enabled, the chart emits a `PrometheusRule` aligned with `deploy/k8s/alerts/fund-worker-slo-rules.yaml`.

Tune routing labels:

```yaml
prometheusRules:
  enabled: true
  alertTeam: platform
  alertSeverity: warning
  criticalSeverity: critical
  extraAlertLabels:
    region: us-east-1
  additionalLabels:
    release: coherence-fund
```

See `docs/ops/runbooks/production_observability_rollout.md` for rollout order and Alertmanager integration.

## Production values and provider alignment

`values-prod.yaml` sets explicit `COHERENCE_FUND_SECRET_MANAGER_PROVIDER` / `COHERENCE_FUND_AWS_REGION` examples; switch to `gcp` or `vault` per cluster and supply credentials via `secretEnv` or external Secrets (see header comments in `values.yaml`). Recurring CI verification of alert routing templates and example on-call policy: `.github/workflows/oncall-route-verification.yml` (artifact **`oncall-release-readiness`**, including incident follow-up checklist; optional scheduled live drill vars in `docs/ops/runbooks/production_observability_rollout.md`). Register each environment’s PagerDuty escalation policy, Opsgenie policy, or Alertmanager receiver in `deploy/ops/oncall-route-policy.example.json` (private copy) and run `deploy/scripts/verify_oncall_route_policy.py` locally.

