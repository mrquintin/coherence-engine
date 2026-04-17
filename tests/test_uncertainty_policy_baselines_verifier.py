"""Tests for deploy/scripts/verify_uncertainty_policy_baselines.py."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "verify_uncertainty_policy_baselines.py"
EXAMPLE_BASELINES = REPO_ROOT / "deploy" / "ops" / "uncertainty-governance-policy-baselines.example.json"
POLICY = REPO_ROOT / "data" / "governed" / "uncertainty_governance_policy.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_verifier(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env=os.environ,
        capture_output=True,
        text=True,
        check=False,
    )


def test_example_baselines_exits_zero_against_repo_policy():
    proc = _run_verifier("--baselines", str(EXAMPLE_BASELINES))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["drift_detected"] is False
    assert out["alert"] is None
    assert out["actual_policy_sha256"] == _sha256_file(POLICY)
    for row in out["environments"]:
        assert row["outcome"] == "match"
        assert row["drift"] is False


def test_json_out_written(tmp_path):
    out_path = tmp_path / "v.json"
    proc = _run_verifier("--baselines", str(EXAMPLE_BASELINES), "--json-out", str(out_path))
    assert proc.returncode == 0
    disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert disk["ok"] is True
    assert disk["error_count"] == 0


def test_drift_exits_nonzero_with_alert(tmp_path):
    good_hash = _sha256_file(POLICY)
    bad_hash = "0" * 64
    assert bad_hash != good_hash
    baseline = {
        "schema_version": 1,
        "policy_path": "data/governed/uncertainty_governance_policy.json",
        "environments": {
            "shadow": {
                "expected_policy_sha256": bad_hash,
                "ownership": {
                    "owning_team": "t",
                    "policy_owner": "o",
                },
                "change_review": {
                    "last_baseline_approved_at": "2026-01-01",
                    "approval_change_id": "x",
                },
            }
        },
    }
    bpath = tmp_path / "baselines.json"
    bpath.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    proc = _run_verifier("--baselines", str(bpath))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert out["drift_detected"] is True
    assert out["alert"] == "drift"
    assert any("drift" in e.lower() for e in out["errors"])


def test_invalid_baseline_schema_exits_nonzero(tmp_path):
    bpath = tmp_path / "bad.json"
    bpath.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    proc = _run_verifier("--baselines", str(bpath))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert out["alert"] == "invalid_baseline"


def test_missing_baselines_file(tmp_path):
    missing = tmp_path / "nope.json"
    proc = _run_verifier("--baselines", str(missing))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert "not found" in out["errors"][0].lower()


def test_reject_example_baseline_path_flag():
    proc = _run_verifier("--reject-example-baseline-path", "--baselines", str(EXAMPLE_BASELINES))
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["ok"] is False
    assert any("example" in e.lower() for e in out["errors"])

    proc_ok = _run_verifier("--baselines", str(EXAMPLE_BASELINES))
    assert proc_ok.returncode == 0
