#!/usr/bin/env bash
# Process-launcher for the fund Arq worker. Used by the systemd unit
# and by ad-hoc operator runs. Sources /etc/default/coherence-fund (or
# DEPLOY_ENV_FILE if set), validates the minimum env block, then execs
# the python entrypoint so signals reach the worker without a shell
# trampoline.

set -euo pipefail

ENV_FILE="${DEPLOY_ENV_FILE:-/etc/default/coherence-fund}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

: "${WORKER_BACKEND:=arq}"
: "${REDIS_URL:?REDIS_URL must be set for the Arq worker}"
: "${ARQ_QUEUE_PREFIX:=coherence_fund}"

export WORKER_BACKEND REDIS_URL ARQ_QUEUE_PREFIX

PYTHON_BIN="${PYTHON_BIN:-/opt/coherence_engine/venv/bin/python}"

exec "${PYTHON_BIN}" -m coherence_engine.server.fund.workers.arq_worker
