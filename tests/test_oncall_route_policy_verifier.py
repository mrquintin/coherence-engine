"""Tests for deploy/scripts/verify_oncall_route_policy.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "verify_oncall_route_policy.py"
EXAMPLE_POLICY = REPO_ROOT / "deploy" / "ops" / "oncall-route-policy.example.json"


def _run_verifier(*args: str, **env_updates: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_updates}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_example_policy_exits_zero():
    proc = _run_verifier(
        "--policy",
        str(EXAMPLE_POLICY),
        "--fail-on-stale-escalation-ownership",
        "--fail-on-stale-oncall-route-policy",
        "--fail-on-stale-verification-evidence",
        "--require-escalation-rotation-ref",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["error_count"] == 0
    assert out["policy_freshness"]["outcome"] == "ok"
    assert out["verification_evidence"]["outcome"] == "ok"


def test_example_policy_json_out(tmp_path):
    out_path = tmp_path / "v.json"
    proc = _run_verifier(
        "--policy",
        str(EXAMPLE_POLICY),
        "--json-out",
        str(out_path),
        "--fail-on-stale-escalation-ownership",
        "--fail-on-stale-oncall-route-policy",
        "--fail-on-stale-verification-evidence",
        "--require-escalation-rotation-ref",
    )
    assert proc.returncode == 0
    disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert disk["ok"] is True
    assert "staleness" in disk
    assert disk["staleness"].get("outcome") == "ok"
    assert disk["policy_freshness"].get("outcome") == "ok"
    assert disk["verification_evidence"].get("outcome") == "ok"
    assert "rotation_check" in disk
    assert disk["rotation_check"].get("outcome") == "ok"


def test_invalid_json_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    proc = _run_verifier("--policy", str(bad))
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["ok"] is False
    assert body["errors"]


def test_pagerduty_without_escalation_fails(tmp_path):
    pol = {
        "schema_version": "1",
        "environments": [
            {
                "name": "x",
                "secret_manager_provider": "aws",
                "oncall_provider": "pagerduty",
                "receiver_ref": "r",
                "escalation_policy_ref": "",
                "in_process_ops_alert_router_mode": "none",
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p))
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert any("escalation_policy_ref" in e for e in body["errors"])


def test_duplicate_environment_names_fail(tmp_path):
    pol = {
        "schema_version": "1",
        "environments": [
            {
                "name": "prod",
                "secret_manager_provider": "aws",
                "oncall_provider": "slack",
                "receiver_ref": "a",
                "escalation_policy_ref": "",
            },
            {
                "name": "prod",
                "secret_manager_provider": "aws",
                "oncall_provider": "slack",
                "receiver_ref": "b",
                "escalation_policy_ref": "",
            },
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p))
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert any("duplicate" in e.lower() for e in body["errors"])


def test_check_env_requires_env_name():
    proc = _run_verifier("--policy", str(EXAMPLE_POLICY), "--check-env")
    assert proc.returncode == 2


def test_check_env_mismatch_secret_manager(tmp_path):
    pol = {
        "schema_version": "1",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "gcp",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "in_process_ops_alert_router_mode": "none",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--env",
        "dev",
        "--check-env",
        COHERENCE_FUND_SECRET_MANAGER_PROVIDER="aws",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["env_check_errors"]


def test_check_env_matches(tmp_path):
    pol = {
        "schema_version": "1",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "in_process_ops_alert_router_mode": "none",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--env",
        "dev",
        "--check-env",
        COHERENCE_FUND_SECRET_MANAGER_PROVIDER="aws",
    )
    assert proc.returncode == 0


def test_strict_fails_on_warnings(tmp_path):
    pol = {
        "schema_version": "1",
        "environments": [],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p), "--strict")
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["warning_count"] > 0


def test_fail_on_stale_missing_review_date(tmp_path):
    pol = {
        "schema_version": "1",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--fail-on-stale-escalation-ownership",
        "--reference-time",
        "2026-04-09T12:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["staleness"]["outcome"] == "missing_review_date"
    assert any("escalation_ownership_reviewed_at" in e for e in body["errors"])


def test_fail_on_stale_oncall_route_policy_missing(tmp_path):
    pol = {
        "schema_version": "1",
        "escalation_ownership_reviewed_at": "2026-04-01",
        "verification_evidence_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--fail-on-stale-oncall-route-policy",
        "--reference-time",
        "2026-04-09T12:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["policy_freshness"]["outcome"] == "missing_review_date"
    assert any("oncall_route_policy_reviewed_at" in e for e in body["errors"])


def test_fail_on_stale_verification_evidence_missing(tmp_path):
    pol = {
        "schema_version": "1",
        "escalation_ownership_reviewed_at": "2026-04-01",
        "oncall_route_policy_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--fail-on-stale-verification-evidence",
        "--reference-time",
        "2026-04-09T12:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["verification_evidence"]["outcome"] == "missing_review_date"
    assert any("verification_evidence_reviewed_at" in e for e in body["errors"])


def test_fail_on_stale_oncall_route_policy_age_exceeded(tmp_path):
    pol = {
        "schema_version": "1",
        "oncall_route_policy_reviewed_at": "2020-01-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--fail-on-stale-oncall-route-policy",
        "--max-oncall-route-policy-age-days",
        "30",
        "--reference-time",
        "2026-04-09T00:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["policy_freshness"]["stale"] is True
    assert body["policy_freshness"]["outcome"] == "stale"


def test_fail_on_stale_verification_evidence_age_exceeded(tmp_path):
    pol = {
        "schema_version": "1",
        "verification_evidence_reviewed_at": "2020-01-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--fail-on-stale-verification-evidence",
        "--max-verification-evidence-age-days",
        "30",
        "--reference-time",
        "2026-04-09T00:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["verification_evidence"]["stale"] is True
    assert body["verification_evidence"]["outcome"] == "stale"


def test_fail_on_stale_age_exceeded(tmp_path):
    pol = {
        "schema_version": "1",
        "escalation_ownership_reviewed_at": "2020-01-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--fail-on-stale-escalation-ownership",
        "--max-escalation-ownership-age-days",
        "30",
        "--reference-time",
        "2026-04-09T00:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["staleness"]["stale"] is True
    assert body["staleness"]["outcome"] == "stale"


def test_require_escalation_rotation_ref_missing_fails(tmp_path):
    pol = {
        "schema_version": "1",
        "escalation_ownership_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "prod",
                "secret_manager_provider": "aws",
                "oncall_provider": "pagerduty",
                "receiver_ref": "svc",
                "escalation_policy_ref": "P1",
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier(
        "--policy",
        str(p),
        "--require-escalation-rotation-ref",
        "--reference-time",
        "2026-04-09T00:00:00Z",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["rotation_check"]["outcome"] == "failed"
    assert any("escalation_rotation_ref" in e for e in body["errors"])


def test_invalid_escalation_ownership_max_age_days(tmp_path):
    pol = {
        "schema_version": "1",
        "escalation_ownership_max_age_days": 0,
        "escalation_ownership_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p))
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert any("escalation_ownership_max_age_days" in e for e in body["errors"])


def test_invalid_oncall_route_policy_max_age_days(tmp_path):
    pol = {
        "schema_version": "1",
        "oncall_route_policy_max_age_days": 0,
        "oncall_route_policy_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p))
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert any("oncall_route_policy_max_age_days" in e for e in body["errors"])


def test_invalid_verification_evidence_max_age_days(tmp_path):
    pol = {
        "schema_version": "1",
        "verification_evidence_max_age_days": -1,
        "verification_evidence_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p))
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert any("verification_evidence_max_age_days" in e for e in body["errors"])


def test_invalid_reference_time_exit_code(tmp_path):
    pol = {
        "schema_version": "1",
        "escalation_ownership_reviewed_at": "2026-04-01",
        "environments": [
            {
                "name": "dev",
                "secret_manager_provider": "aws",
                "oncall_provider": "alertmanager",
                "receiver_ref": "am",
                "escalation_policy_ref": "",
                "prometheus_alert_route_labels": {"service": "coherence-fund"},
            }
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pol), encoding="utf-8")
    proc = _run_verifier("--policy", str(p), "--reference-time", "not-a-date")
    assert proc.returncode == 2
