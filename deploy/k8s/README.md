# Kubernetes Deployment Bundle

This bundle deploys:

- API deployment + service
- Outbox worker deployment
- Scoring worker deployment
- Migration job
- HPA (API + outbox worker + scoring worker)
- PodDisruptionBudget (API + outbox worker + scoring worker)
- NetworkPolicy (default deny + targeted allow rules)
- ConfigMap/Secret templates

## Prerequisites

- Container image published (replace `ghcr.io/your-org/coherence-engine:latest`)
- PostgreSQL reachable
- Selected transport reachable (Kafka, SQS, or Redis)
- Kustomize or `kubectl apply -k`

## Configure

1. Edit `secret-template.yaml` with real values.
2. Edit `configmap-env-template.yaml` to select backend:
   - `COHERENCE_FUND_OUTBOX_BACKEND=kafka|sqs|redis`
3. Replace image references in:
   - `api-deployment.yaml`
   - `worker-deployment.yaml`
   - `scoring-worker-deployment.yaml`
   - `migrate-job.yaml`

## Deploy

Required preflight before apply:

```bash
make preflight-secret-manager
```

```bash
kubectl apply -k deploy/k8s
```

Production overlay:

```bash
kubectl apply -k deploy/k8s/overlays/prod
```

Ready-to-apply production broker overlays:

```bash
# Redis
kubectl apply -k deploy/k8s/overlays/prod-redis

# SQS
kubectl apply -k deploy/k8s/overlays/prod-sqs
```

Broker patch files are also available in:

- `deploy/k8s/overlays/prod/brokers/kafka.yaml`
- `deploy/k8s/overlays/prod/brokers/redis.yaml`
- `deploy/k8s/overlays/prod/brokers/sqs.yaml`

See `deploy/k8s/overlays/prod/README.md` for patch activation.

Recurring on-call route verification in CI (safe by default; optional scheduled live webhook and artifact checklist): `.github/workflows/oncall-route-verification.yml` — see `docs/ops/runbooks/production_observability_rollout.md`.

## Verify

```bash
kubectl -n coherence-fund get pods
kubectl -n coherence-fund get svc
kubectl -n coherence-fund logs deploy/coherence-fund-worker -f
kubectl -n coherence-fund logs deploy/coherence-fund-scoring-worker -f
kubectl -n coherence-fund logs deploy/coherence-fund-api -f
kubectl -n coherence-fund exec deploy/coherence-fund-api -- curl -fsS http://127.0.0.1:8010/api/v1/secret-manager/ready
```

## Probes

API probes:

- liveness: `GET /live`
- readiness: `GET /ready`

Outbox worker probes:

- exec probe using `python -m coherence_engine.server.fund.worker_healthcheck`
- validates DB + selected backend connectivity

Scoring worker probes:

- exec probe using `python -m coherence_engine.server.fund.scoring_worker --healthcheck`
- validates database connectivity

## Security and Availability Controls

- HorizontalPodAutoscaler for API, outbox worker, and scoring worker
- PodDisruptionBudget for API, outbox worker, and scoring worker
- NetworkPolicy resources included in `kustomization.yaml`

## Optional worker SLO alerts (Prometheus Operator)

`deploy/k8s/alerts/fund-worker-slo-rules.yaml` defines a `PrometheusRule` for `coherence_fund_*` metrics. It is **not** wired into the default `kustomization.yaml` (additive bundle). Edit `metadata.namespace` and alert labels, then apply:

```bash
kubectl apply -f deploy/k8s/alerts/fund-worker-slo-rules.yaml
```

See `docs/ops/runbooks/production_observability_rollout.md` and `docs/ops/slo_threshold_standards.md` for thresholds and routing.

## Recurring route verification (CI)

`configmap-env-template.yaml` documents which variables belong in ConfigMap vs Secret (webhook URLs for `COHERENCE_FUND_OPS_ALERT_*` must not live in ConfigMap). Repository workflow `.github/workflows/oncall-route-verification.yml` validates templates, example on-call policy JSON, and alert routing weekly without external dependencies; download **`oncall-release-readiness`** artifacts for verification JSON and drill evidence. Per-environment PagerDuty/Opsgenie/Alertmanager mapping template: `deploy/ops/oncall-route-policy.example.json`.

