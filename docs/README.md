# Strategy and Planning Docs

This directory contains planning and prompting documents for evolving the Coherence Engine into an automated, quantitative pre-seed fund workflow.

- `AUTOMATED_PRESEED_FUND_BLUEPRINT.md`: target-state architecture, formulas, and end-to-end automation flow.
- `IMPLEMENTATION_ROADMAP_AND_BACKLOG.md`: phased engineering roadmap, milestones, and execution backlog.
- `specs/README.md`: execution-ready technical spec pack (policy math, event schemas, API contracts, OpenAPI).
- `SECRET_MANAGER_ROTATION_RUNBOOK.md`: provider policy, bootstrap procedure, and operational key-rotation runbook.
- `PARALLEL_DELEGATION.md`: multi-subagent prompt splitting, parallel delegation, and agent-list configuration.
- `ops/README.md`: worker ops telemetry sinks, Prometheus metric names, Grafana dashboard import, alert rule pointers, and Alertmanager routing notes.
- `ops/slo_threshold_standards.md`: baseline SLO table and env threshold defaults for workers.
- `ops/runbooks/production_observability_rollout.md`: production observability rollout, verification, recurring CI on-call route drills (`oncall-route-verification.yml`), and per-environment on-call policy / artifact guidance (`deploy/ops/oncall-route-policy.example.json`, `deploy/scripts/verify_oncall_route_policy.py`).

Recommended reading order:

1. Blueprint
2. Roadmap and Backlog
3. Technical Spec Pack (`specs/`)

