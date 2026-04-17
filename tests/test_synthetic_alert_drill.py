"""Tests for deploy/scripts/synthetic_alert_drill.py (subprocess + env)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRILL_SCRIPT = REPO_ROOT / "deploy" / "scripts" / "synthetic_alert_drill.py"


def _run_drill(*args: str, **env_updates: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.update(env_updates)
    return subprocess.run(
        [sys.executable, str(DRILL_SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_drill_exits_zero_when_mode_none():
    proc = _run_drill("--json", COHERENCE_FUND_OPS_ALERT_ROUTER_MODE="none")
    assert proc.returncode == 0
    row = json.loads(proc.stdout.strip())
    assert row["ok"] is True


def test_drill_exits_zero_when_mode_unset():
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    env.pop("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", None)
    proc = subprocess.run(
        [sys.executable, str(DRILL_SCRIPT), "--json"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    row = json.loads(proc.stdout.strip())
    assert row["ok"] is True


def test_drill_file_mode_delivers(tmp_path):
    out = tmp_path / "drill.jsonl"
    proc = _run_drill(
        "--json",
        COHERENCE_FUND_OPS_ALERT_ROUTER_MODE="file",
        COHERENCE_FUND_OPS_ALERT_FILE_PATH=str(out),
        COHERENCE_FUND_OPS_ALERT_COOLDOWN_SECONDS="0",
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout.strip())
    assert body["ok"] is True
    assert body["channel"] == "file"
    assert out.read_text(encoding="utf-8").strip()


def test_verify_only_fails_on_misconfigured_webhook():
    proc = _run_drill(
        "--verify-only",
        COHERENCE_FUND_OPS_ALERT_ROUTER_MODE="webhook",
        COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL="",
    )
    assert proc.returncode == 1
    assert proc.stderr.strip() or "WEBHOOK" in proc.stdout


def test_strict_verify_env_blocks_drill_when_issues(tmp_path):
    out = tmp_path / "x.jsonl"
    proc = _run_drill(
        "--json",
        COHERENCE_FUND_OPS_ALERT_ROUTER_MODE="webhook",
        COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL="",
        COHERENCE_FUND_OPS_ALERT_DRILL_STRICT_VERIFY="true",
        COHERENCE_FUND_OPS_ALERT_FILE_PATH=str(out),
    )
    assert proc.returncode == 1
    row = json.loads(proc.stdout.strip())
    assert row["ok"] is False
    assert row["phase"] == "verify"
