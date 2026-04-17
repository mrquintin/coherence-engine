"""Tests for deploy/scripts/report_governance_attestation_age.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "report_governance_attestation_age.py"
ROUTE_SCRIPT = REPO_ROOT / "deploy" / "scripts" / "route_governance_attestation_escalation.py"
EXAMPLE_BASELINES = REPO_ROOT / "deploy" / "ops" / "uncertainty-governance-policy-baselines.example.json"


def _load_route_module():
    spec = importlib.util.spec_from_file_location("route_governance_attestation_escalation", ROUTE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_route(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROUTE_SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_report_writes_json_and_markdown(tmp_path: Path) -> None:
    j = tmp_path / "out.json"
    m = tmp_path / "out.md"
    proc = _run(
        "report",
        "--baselines",
        str(EXAMPLE_BASELINES),
        "--as-of-date",
        "2026-04-09",
        "--json-out",
        str(j),
        "--markdown-out",
        str(m),
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not proc.stdout.strip(), "stdout should be quiet when --json-out is set"
    data = json.loads(j.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["report_kind"] == "governance_baseline_approval_age"
    assert data["as_of_utc_date"] == "2026-04-09"
    assert len(data["environments"]) >= 1
    body = m.read_text(encoding="utf-8")
    assert "Governance attestation aging" in body
    assert "| shadow |" in body or "| canary |" in body


def test_report_stale_status_with_old_approval_date(tmp_path: Path) -> None:
    baseline = {
        "schema_version": 1,
        "policy_path": "data/governed/uncertainty_governance_policy.json",
        "environments": {
            "shadow": {
                "expected_policy_sha256": "0" * 64,
                "ownership": {"owning_team": "t", "policy_owner": "o"},
                "change_review": {
                    "last_baseline_approved_at": "2020-01-01",
                    "approval_change_id": "x",
                },
            }
        },
    }
    bpath = tmp_path / "b.json"
    bpath.write_text(json.dumps(baseline), encoding="utf-8")
    j = tmp_path / "r.json"
    proc = _run(
        "report",
        "--baselines",
        str(bpath),
        "--as-of-date",
        "2026-04-09",
        "--max-age-days",
        "90",
        "--json-out",
        str(j),
    )
    assert proc.returncode == 0
    data = json.loads(j.read_text(encoding="utf-8"))
    row = data["environments"][0]
    assert row["status"] == "stale"
    assert row["age_days"] > 90


def test_validate_ownership_attestation_ok(tmp_path: Path) -> None:
    proc = _run(
        "validate-ownership-attestation",
        env={
            "EFF": "2026-03-01",
            "MAX_VAR": "90",
            "REM_VAR": "14",
            "ESCALATION_OUT": str(tmp_path / "esc.json"),
            "GOVERNANCE_ATTESTATION_WORKFLOW_FILE": "unit-test.yml",
        },
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "Ownership attestation age OK" in proc.stdout


def test_validate_ownership_attestation_stale_writes_escalation(tmp_path: Path) -> None:
    esc = tmp_path / "governance_attestation_escalation.json"
    proc = _run(
        "validate-ownership-attestation",
        env={
            "EFF": "2020-01-01",
            "MAX_VAR": "90",
            "REM_VAR": "14",
            "ESCALATION_OUT": str(esc),
            "GOVERNANCE_ATTESTATION_WORKFLOW_FILE": "unit-test.yml",
        },
    )
    assert proc.returncode == 1
    assert "governance-attestation-escalation" in proc.stdout
    rec = json.loads(esc.read_text(encoding="utf-8"))
    assert rec["record_type"] == "governance_attestation_escalation"
    assert rec["breach"]["kind"] == "ownership_attestation_stale"
    assert rec["breach"]["age_days"] is not None
    assert "promotion_context" in rec
    assert rec["promotion_context"]["promotion_environment"] is None
    paths = rec.get("escalation_paths") or []
    assert any(p.get("artifact_name") == "governance-attestation-escalation" for p in paths)


def test_validate_escalation_includes_promotion_context_from_env(tmp_path: Path) -> None:
    esc = tmp_path / "esc.json"
    proc = _run(
        "validate-ownership-attestation",
        env={
            "EFF": "2020-01-01",
            "MAX_VAR": "90",
            "REM_VAR": "14",
            "ESCALATION_OUT": str(esc),
            "GOVERNANCE_ATTESTATION_WORKFLOW_FILE": "unit-test.yml",
            "GOVERNANCE_PROMOTION_ENV": "prod",
            "GOVERNANCE_GITHUB_ENVIRONMENT": "uncertainty-governance-prod",
        },
    )
    assert proc.returncode == 1
    rec = json.loads(esc.read_text(encoding="utf-8"))
    assert rec["promotion_context"]["promotion_environment"] == "prod"
    assert rec["promotion_context"]["github_environment"] == "uncertainty-governance-prod"


def test_route_escalation_missing_file_exits_zero(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    proc = _run_route("--escalation-json", str(missing))
    assert proc.returncode == 0
    assert "no file" in proc.stderr


def test_route_escalation_malformed_json_exits_zero(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    proc = _run_route("--escalation-json", str(bad))
    assert proc.returncode == 0
    assert "invalid JSON" in proc.stderr


def test_route_escalation_file_sink_appends_ndjson(tmp_path: Path) -> None:
    esc = tmp_path / "esc.json"
    esc.write_text(
        json.dumps(
            {
                "record_type": "governance_attestation_escalation",
                "breach": {"kind": "ownership_attestation_stale"},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "routed.ndjson"
    proc = _run_route(
        "--escalation-json",
        str(esc),
        "--sink",
        "file",
        env={"GOVERNANCE_ESCALATION_FILE_PATH": str(out)},
    )
    assert proc.returncode == 0, proc.stderr
    line = out.read_text(encoding="utf-8").strip().splitlines()[-1]
    row = json.loads(line)
    assert row["sink"] == "file"
    assert row["escalation"]["breach"]["kind"] == "ownership_attestation_stale"


def test_route_escalation_webhook_dry_run_no_network(tmp_path: Path) -> None:
    esc = tmp_path / "esc.json"
    esc.write_text(
        json.dumps({"record_type": "governance_attestation_escalation", "breach": {}}),
        encoding="utf-8",
    )
    proc = _run_route(
        "--escalation-json",
        str(esc),
        "--sink",
        "webhook",
        env={
            "GOVERNANCE_ESCALATION_WEBHOOK_URL": "https://example.invalid/hook",
            "GOVERNANCE_ESCALATION_DRY_RUN": "1",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "dry-run" in proc.stderr


def test_route_escalation_webhook_post_failure_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    esc = tmp_path / "esc.json"
    esc.write_text(
        json.dumps({"record_type": "governance_attestation_escalation", "breach": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOVERNANCE_ESCALATION_WEBHOOK_URL", "https://example.invalid/hook")
    mod = _load_route_module()
    with patch.object(mod.urllib.request, "urlopen", side_effect=mod.urllib.error.URLError("boom")):
        rc = mod.main(
            [
                "--escalation-json",
                str(esc),
                "--sink",
                "webhook",
                "--timeout-seconds",
                "1",
            ]
        )
    assert rc == 0


def test_route_map_resolves_webhook_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_route_module()
    mpath = tmp_path / "map.json"
    mpath.write_text(
        json.dumps(
            {
                "by_promotion_environment": {
                    "canary": {"webhook_environment_variable": "TEST_HOOK_URL_VAR"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_HOOK_URL_VAR", "https://example.test/x")
    url = mod._resolve_webhook_url(
        mod._load_routing_map(mpath),
        github_environment="",
        promotion_env="canary",
    )
    assert url == "https://example.test/x"


def test_route_promotion_context_fills_resolution_when_env_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    esc = tmp_path / "esc.json"
    esc.write_text(
        json.dumps(
            {
                "record_type": "governance_attestation_escalation",
                "promotion_context": {
                    "promotion_environment": "canary",
                    "github_environment": "uncertainty-governance-canary",
                },
                "breach": {},
            }
        ),
        encoding="utf-8",
    )
    mod = _load_route_module()
    mpath = tmp_path / "map.json"
    mpath.write_text(
        json.dumps(
            {
                "by_github_environment": {
                    "uncertainty-governance-canary": {
                        "webhook_environment_variable": "CTX_HOOK_URL",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CTX_HOOK_URL", "https://ctx.example/h")
    monkeypatch.setenv("GOVERNANCE_ESCALATION_DRY_RUN", "1")
    rc = mod.main(
        [
            "--escalation-json",
            str(esc),
            "--map-json",
            str(mpath),
            "--sink",
            "webhook",
        ]
    )
    assert rc == 0
