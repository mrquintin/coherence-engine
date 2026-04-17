#!/usr/bin/env bash
set -euo pipefail

READY_URL="${COHERENCE_FUND_READY_URL:-http://127.0.0.1:8010/ready}"
SECRET_MANAGER_READY_URL="${COHERENCE_FUND_SECRET_MANAGER_READY_URL:-http://127.0.0.1:8010/api/v1/secret-manager/ready}"
API_UNIT="${COHERENCE_FUND_API_UNIT:-coherence-fund-api.service}"
WORKER_UNIT="${COHERENCE_FUND_WORKER_UNIT:-coherence-fund-worker.service}"
SCORING_WORKER_UNIT="${COHERENCE_FUND_SCORING_WORKER_UNIT:-coherence-fund-scoring-worker.service}"
TIMEOUT="${COHERENCE_FUND_WATCHDOG_TIMEOUT_SECONDS:-5}"

_emit_alert() {
  echo "COHERENCE_FUND_WATCHDOG_ALERT reason=${1}" >&2
}

_restart_api_and_workers() {
  systemctl restart "${API_UNIT}"
  systemctl restart "${WORKER_UNIT}"
  systemctl restart "${SCORING_WORKER_UNIT}"
}

if ! systemctl is-active --quiet "${API_UNIT}"; then
  _emit_alert "api_unit_inactive"
  systemctl restart "${API_UNIT}"
  exit 1
fi

if ! curl -fsS --max-time "${TIMEOUT}" "${READY_URL}" >/dev/null; then
  _emit_alert "api_ready_probe_failed"
  _restart_api_and_workers
  exit 1
fi

if ! curl -fsS --max-time "${TIMEOUT}" "${SECRET_MANAGER_READY_URL}" >/dev/null; then
  _emit_alert "secret_manager_ready_probe_failed"
  _restart_api_and_workers
  exit 1
fi

exit 0
