"""Tests for deployment secret-manager preflight script."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "secret_manager_preflight.py"


def _base_env() -> dict:
    env = os.environ.copy()
    # Keep tests isolated from local machine credentials/settings.
    for k in [
        "COHERENCE_FUND_SECRET_MANAGER_PROVIDER",
        "COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY",
        "COHERENCE_FUND_SECRET_MANAGER_ALLOW_STATIC_CREDENTIALS",
        "COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED",
        "COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF",
        "COHERENCE_FUND_VAULT_ADDR",
        "COHERENCE_FUND_VAULT_TOKEN",
        "COHERENCE_FUND_VAULT_TOKEN_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]:
        env.pop(k, None)
    return env


def test_preflight_fails_when_provider_disabled_by_default():
    env = _base_env()
    env["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    env["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", "json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 1
    assert "preflight" in proc.stdout.lower() or "error" in proc.stdout.lower()


def test_preflight_allows_disabled_when_explicitly_permitted():
    env = _base_env()
    env["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    env["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--allow-disabled", "--output", "json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert '"ok": true' in proc.stdout.lower()

