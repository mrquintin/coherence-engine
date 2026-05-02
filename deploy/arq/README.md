# deploy/arq — Arq worker artifacts

Production deployment artifacts for the fund Arq background worker
(`server/fund/workers/arq_worker.py`). See `docs/ops/worker_runbook.md`
for the operator runbook.

| File | Purpose |
|------|---------|
| `coherence-fund-arq-worker.service` | systemd unit |
| `run_arq_worker.sh` | shell launcher used by the systemd unit and ad-hoc runs |
| `values.yaml` | Helm values block (paste into the umbrella chart) |
| `upstash.env.example` | Upstash Redis env template |

The polling worker (`deploy/systemd/coherence-fund-scoring-worker.service`)
remains the failsafe — `WORKER_BACKEND=poll` reverts to it without a
code deploy.
