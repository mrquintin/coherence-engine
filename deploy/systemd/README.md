# systemd Deployment Bundle

This bundle runs API, outbox dispatcher worker, and scoring queue worker as system services.

## Files

- `coherence-fund-api.service`
- `coherence-fund-worker.service`
- `coherence-fund-scoring-worker.service`
- `coherence-fund-health-watchdog.service`
- `coherence-fund-health-watchdog.timer`
- `coherence-fund-health-watchdog.sh`
- `coherence-fund.env.example`

Worker ops snapshots always go to journald/log; optional `COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH` (JSONL) and `COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH` (node_exporter textfile) add local sinks without outbound network. See `docs/ops/README.md` and `docs/ops/grafana/fund_worker_slo_dashboard.json`.

In-process threshold env vars default to `0` (disabled) in `coherence-fund.env.example`; optional runtime alert routing keys use `COHERENCE_FUND_OPS_ALERT_*` and are read by worker services when enabled. Rollout: `docs/ops/runbooks/production_observability_rollout.md`.

Recurring **synthetic** alert routing checks (no required outbound network by default) run in GitHub Actions: `.github/workflows/oncall-route-verification.yml` (artifacts: policy verification JSON, drill evidence JSONL, release-readiness summary, incident follow-up checklist; optional live drill and quiet-window controls in `docs/ops/runbooks/production_observability_rollout.md`). Provider alignment for secret managers (`aws` / `gcp` / `vault`) is commented in `coherence-fund.env.example`. On-call escalation/receiver mapping is documented in `deploy/ops/oncall-route-policy.example.json` and validated with `deploy/scripts/verify_oncall_route_policy.py`.

## Install Steps

1. Create app user and paths:

```bash
sudo useradd --system --home /opt/coherence_engine --shell /usr/sbin/nologin coherence || true
sudo mkdir -p /opt/coherence_engine
sudo chown -R coherence:coherence /opt/coherence_engine
```

2. Copy project and create venv:

```bash
cd /opt/coherence_engine
python3 -m venv venv
source venv/bin/activate
pip install -e ".[full,fund-workers]"
```

3. Configure environment:

```bash
sudo cp deploy/systemd/coherence-fund.env.example /etc/default/coherence-fund
sudo nano /etc/default/coherence-fund
```

4. Run DB migrations:

```bash
cd /opt/coherence_engine
source venv/bin/activate
alembic upgrade head
```

5. Required preflight (must pass before enable/start):

```bash
cd /opt/coherence_engine
source venv/bin/activate
make preflight-secret-manager
```

6. Install units:

```bash
sudo cp deploy/systemd/coherence-fund-api.service /etc/systemd/system/
sudo cp deploy/systemd/coherence-fund-worker.service /etc/systemd/system/
sudo cp deploy/systemd/coherence-fund-scoring-worker.service /etc/systemd/system/
sudo cp deploy/systemd/coherence-fund-health-watchdog.service /etc/systemd/system/
sudo cp deploy/systemd/coherence-fund-health-watchdog.timer /etc/systemd/system/
sudo cp deploy/systemd/coherence-fund-health-watchdog.sh /opt/coherence_engine/deploy/systemd/
sudo chmod +x /opt/coherence_engine/deploy/systemd/coherence-fund-health-watchdog.sh
sudo systemctl daemon-reload
sudo systemctl enable --now coherence-fund-api.service
sudo systemctl enable --now coherence-fund-worker.service
sudo systemctl enable --now coherence-fund-scoring-worker.service
sudo systemctl enable --now coherence-fund-health-watchdog.timer
```

7. Verify:

```bash
systemctl status coherence-fund-api.service
systemctl status coherence-fund-worker.service
systemctl status coherence-fund-scoring-worker.service
systemctl status coherence-fund-health-watchdog.timer
systemctl list-timers | grep coherence-fund-health-watchdog
curl -f http://127.0.0.1:8010/live
curl -f http://127.0.0.1:8010/ready
curl -f http://127.0.0.1:8010/api/v1/secret-manager/ready
```

