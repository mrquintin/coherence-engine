"""Unit tests for deploy/scripts/validate_oncall_tracker_handoff.py (stdlib governance + contracts)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "scripts" / "validate_oncall_tracker_handoff.py"
EXAMPLE_POLICY = ROOT / "deploy" / "ops" / "oncall_tracker_handoff_policy.example.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_oncall_tracker_handoff", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load_module()


def _minimal_ticket(**overrides):
    base = {
        "schema": v.TICKET_TEMPLATE_SCHEMA,
        "environment": "staging",
        "issue_title_suggested": "t",
        "labels": ["a"],
        "github_actions": {
            "workflow": "oncall-route-verification.yml",
            "repository": "o/r",
            "run_url": "https://github.com/o/r/actions/runs/1",
        },
    }
    base.update(overrides)
    return base


def test_clamp_effective_policy_caps_attempts_and_backoff():
    eff = v.clamp_effective_policy(
        {
            "max_attempts": 99,
            "backoff_initial_seconds": 0.01,
            "backoff_max_seconds": 500,
            "retryable_http_statuses": [999, 200, 500],
            "idempotency_mode": "nope",
        }
    )
    assert eff["max_attempts"] == v.MAX_ATTEMPTS_CAP
    assert eff["backoff_initial_seconds"] == pytest.approx(v.MIN_BACKOFF_INITIAL)
    assert eff["backoff_max_seconds"] <= v.MAX_BACKOFF_MAX_CAP
    assert 500 in eff["retryable_http_statuses"]
    assert 999 not in eff["retryable_http_statuses"]
    assert eff["idempotency_mode"] == "run_env_payload"


def test_effective_policy_per_environment_merge():
    doc = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    st = v.effective_policy_for_environment(doc, "staging")
    pr = v.effective_policy_for_environment(doc, "production")
    assert st["max_attempts"] == 4
    assert pr["max_attempts"] == 5
    assert pr["backoff_max_seconds"] >= pr["backoff_initial_seconds"]


def test_policy_drift_vs_builtin_staging_example_file():
    doc = json.loads(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    st = v.effective_policy_for_environment(doc, "staging")
    drift = v.policy_drift_vs_builtin(st)
    assert drift["has_drift"] is False
    pr = v.effective_policy_for_environment(doc, "production")
    drift_p = v.policy_drift_vs_builtin(pr)
    assert drift_p["has_drift"] is True
    fields = {d["field"] for d in drift_p["differences"]}
    assert "max_attempts" in fields


def test_policy_resolution_governance_secret_wins():
    g = v.policy_resolution_governance(True, True, "secret_json")
    assert g["selected_source"] == "secret_json"
    assert g["inputs_evaluated"]["ONCALL_TRACKER_HANDOFF_POLICY_JSON_non_empty"] is True


def test_redact_url_for_audit_strips_query():
    h = v.redact_url_for_audit("https://ex.example/issues/1?token=secret")
    assert h is not None
    assert h["url_host"] == "ex.example"
    assert "token" not in json.dumps(h)


def test_extract_tracker_reconciliation_jira():
    body = json.dumps(
        {"key": "TST-1", "id": "10005", "self": "https://x.atlassian.net/rest/api/3/issue/10005"}
    ).encode()
    r = v.extract_tracker_reconciliation("jira", 201, body)
    assert r["applicable"] is True
    assert r["tracker_issue_key"] == "TST-1"
    assert r["tracker_issue_id_suffix"] == "10005"
    assert r.get("tracker_resource_hint", {}).get("url_host") == "x.atlassian.net"


def test_extract_tracker_reconciliation_github():
    body = json.dumps(
        {
            "id": 123456789012345,
            "number": 42,
            "html_url": "https://github.com/o/r/issues/42",
        }
    ).encode()
    r = v.extract_tracker_reconciliation("github", 200, body)
    assert r["applicable"] is True
    assert r["tracker_issue_number"] == 42
    assert "2345" in r["tracker_issue_id_suffix"]


def test_extract_tracker_reconciliation_non_success():
    r = v.extract_tracker_reconciliation("jira", 500, b"{}")
    assert r["applicable"] is False
    assert r["skip_reason"] == "non_success_http"


def test_validate_ticket_contract_generic_ok():
    ok, errs = v.validate_ticket_contract("generic", _minimal_ticket())
    assert ok and errs == []


def test_validate_ticket_contract_generic_requires_template_schema():
    t = _minimal_ticket(schema="wrong")
    ok, errs = v.validate_ticket_contract("generic", t)
    assert not ok
    assert any("schema" in e for e in errs)


def test_validate_ticket_contract_jira_requires_project_key():
    t = _minimal_ticket()
    ok, errs = v.validate_ticket_contract("jira", t)
    assert not ok
    assert any("tracker_project_key" in e for e in errs)
    t2 = _minimal_ticket(tracker_project_key="PROJ")
    ok2, errs2 = v.validate_ticket_contract("jira", t2)
    assert ok2 and errs2 == []


def test_validate_ticket_contract_github_requires_slash_repo():
    t = _minimal_ticket(github_actions={"repository": "bad"})
    ok, errs = v.validate_ticket_contract("github", t)
    assert not ok
    assert any("OWNER/REPO" in e for e in errs)


def test_idempotency_key_off_returns_none():
    assert v.idempotency_key("off", "1", "staging", "abc") is None
    assert v.idempotency_key("run_env_payload", "1", "staging", "abc") is not None


def test_ci_check_subprocess_ok(tmp_path: Path):
    st = tmp_path / "st.json"
    pr = tmp_path / "pr.json"
    st.write_text(
        json.dumps(_minimal_ticket(environment="staging", tracker_project_key="STG")),
        encoding="utf-8",
    )
    pr.write_text(
        json.dumps(_minimal_ticket(environment="production", tracker_project_key="PRD")),
        encoding="utf-8",
    )
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "ci-check",
            "--repo-root",
            str(ROOT),
            "--policy",
            "deploy/ops/oncall_tracker_handoff_policy.example.json",
            "--staging-payload",
            str(st),
            "--production-payload",
            str(pr),
            "--staging-provider",
            "jira",
            "--production-provider",
            "jira",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert "GOVERNANCE policy_resolution=" in r.stdout
    assert "policy_drift_vs_builtin_defaults=" in r.stdout


def test_script_contains_adapter_markers():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "jira_missing_tracker_project_key" in body
    assert "application/vnd.github+json" in body


def test_ci_check_subprocess_fails_on_bad_jira_contract(tmp_path: Path):
    st = tmp_path / "st.json"
    pr = tmp_path / "pr.json"
    bad = _minimal_ticket(environment="staging")
    bad.pop("tracker_project_key", None)
    st.write_text(json.dumps(bad), encoding="utf-8")
    pr.write_text(json.dumps(_minimal_ticket(environment="production", tracker_project_key="P")), encoding="utf-8")
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "ci-check",
            "--repo-root",
            str(ROOT),
            "--staging-payload",
            str(st),
            "--production-payload",
            str(pr),
            "--staging-provider",
            "jira",
            "--production-provider",
            "jira",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1


def test_ci_check_policy_json_env_overrides_cli_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Secret-style JSON in env wins over --policy (same precedence as `run`)."""
    st = tmp_path / "st.json"
    pr = tmp_path / "pr.json"
    st.write_text(
        json.dumps(_minimal_ticket(environment="staging", tracker_project_key="STG")),
        encoding="utf-8",
    )
    pr.write_text(
        json.dumps(_minimal_ticket(environment="production", tracker_project_key="PRD")),
        encoding="utf-8",
    )
    overlay = {
        "schema": v.POLICY_SCHEMA,
        "defaults": {"max_attempts": 2},
        "environments": {"staging": {}, "production": {}},
    }
    monkeypatch.setenv("ONCALL_TRACKER_HANDOFF_POLICY_JSON", json.dumps(overlay))
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "ci-check",
            "--repo-root",
            str(ROOT),
            "--policy",
            "deploy/ops/oncall_tracker_handoff_policy.example.json",
            "--staging-payload",
            str(st),
            "--production-payload",
            str(pr),
            "--staging-provider",
            "jira",
            "--production-provider",
            "jira",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    monkeypatch.delenv("ONCALL_TRACKER_HANDOFF_POLICY_JSON", raising=False)
    assert r.returncode == 0, r.stderr + r.stdout
    assert '"selected_source": "secret_json"' in r.stdout


def test_writeback_reconciliation_appends_markdown(tmp_path: Path) -> None:
    art = tmp_path / "artifacts" / "oncall"
    art.mkdir(parents=True)
    md_name = "oncall-live-drill-followup-staging.md"
    (art / md_name).write_text("# Follow-up\n\nBody.\n", encoding="utf-8")
    results = tmp_path / "oncall-tracker-handoff-results.json"
    results.write_text(
        json.dumps(
            {
                "schema": "oncall_tracker_handoff_results/v2",
                "environments": [
                    {
                        "environment": "staging",
                        "closure_artifacts": {"issue_body_markdown_artifact": md_name},
                        "response": {
                            "reconciliation": {
                                "applicable": True,
                                "tracker_issue_key": "ABC-99",
                                "tracker_issue_number": 99,
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "writeback-reconciliation",
            "--results-json",
            str(results),
            "--artifacts-dir",
            str(art),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    text_out = (art / md_name).read_text(encoding="utf-8")
    assert "## Tracker reconciliation (automation write-back)" in text_out
    assert "ABC-99" in text_out
    r2 = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "writeback-reconciliation",
            "--results-json",
            str(results),
            "--artifacts-dir",
            str(art),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r2.returncode == 0
    assert text_out.count("## Tracker reconciliation (automation write-back)") == 1
