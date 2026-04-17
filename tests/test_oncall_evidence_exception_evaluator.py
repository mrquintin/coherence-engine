"""Unit tests for deploy/scripts/evaluate_oncall_evidence_exception.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = REPO_ROOT / "deploy" / "scripts" / "evaluate_oncall_evidence_exception.py"


def _load_evaluator():
    spec = importlib.util.spec_from_file_location(
        "evaluate_oncall_evidence_exception", _SCRIPT
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_evaluator()
evaluate = _mod.evaluate
main = _mod.main
_load_policy_file = _mod._load_policy_file

POLICY_EXAMPLE = REPO_ROOT / "deploy" / "ops" / "oncall_evidence_exception_policy.example.json"


def _future_iso() -> str:
    t = datetime.now(timezone.utc) + timedelta(days=7)
    return t.isoformat().replace("+00:00", "Z")


def _past_iso() -> str:
    t = datetime.now(timezone.utc) - timedelta(days=1)
    return t.isoformat().replace("+00:00", "Z")


def _base_state(*, stale: bool) -> dict:
    return {
        "evidence_found": True,
        "stale": stale,
        "artifact_name": "oncall-live-webhook-drill",
        "workflow_file": "oncall-route-verification.yml",
        "branch": "main",
        "max_age_hours": 168.0,
        "evidence_created_at": "2026-01-01T00:00:00Z",
        "age_seconds": 999999.0 if stale else 10.0,
        "workflow_run_id": 1,
        "workflow_run_url": "https://example.com/run",
        "workflow_run_updated_at": "2026-01-01T00:00:00Z",
    }


def test_evaluate_fresh_allows_without_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_JSON", raising=False)
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=False),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "allow"
    assert reason == "fresh_within_window"
    assert trace is None
    assert errs == []


def test_evaluate_stale_denies_without_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_JSON", raising=False)
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "deny"
    assert reason == "stale_no_exception"
    assert trace is None
    assert errs


def test_evaluate_stale_allows_with_valid_snooze(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {
        "schema_version": 1,
        "gate": "release",
        "github_repository": "org/repo",
        "snooze": {
            "expires_at": _future_iso(),
            "approver": "alice@example.com",
            "change_id": "CHG-1",
            "reason": "provider maintenance",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "allow"
    assert reason == "snooze_active"
    assert trace is not None
    assert trace["change_id"] == "CHG-1"
    assert trace["approver"] == "alice@example.com"
    assert trace.get("policy_applied") is False
    assert errs == []


def test_evaluate_expired_snooze_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {
        "schema_version": 1,
        "gate": "release",
        "snooze": {
            "expires_at": _past_iso(),
            "approver": "alice@example.com",
            "change_id": "CHG-1",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "deny"
    assert reason == "exception_invalid_or_expired"
    assert trace is None
    assert any("expired" in e.lower() for e in errs)


def test_evaluate_wrong_gate_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {
        "schema_version": 1,
        "gate": "nonprod",
        "snooze": {
            "expires_at": _future_iso(),
            "approver": "alice@example.com",
            "change_id": "CHG-1",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, _, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "deny"
    assert reason == "exception_invalid_or_expired"
    assert any("gate" in e for e in errs)


def test_evaluate_both_json_and_path_denies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "ex.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", '{"schema_version": 1}')
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", "ex.json")
    d, reason, _, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "deny"
    assert reason == "exception_load_error"
    assert any("Both" in e for e in errs)


def test_main_writes_decision_and_exits_zero_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_base_state(stale=False)), encoding="utf-8")
    out = tmp_path / "decision.json"
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_JSON", raising=False)
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    code = main(
        ["--state", str(state_path), "--decision-out", str(out), "--gate", "release"]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["decision"] == "allow"
    assert payload["reason_code"] == "fresh_within_window"
    assert payload["exception_applied"] is False


def test_main_exits_one_on_stale_no_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_base_state(stale=True)), encoding="utf-8")
    out = tmp_path / "decision.json"
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_JSON", raising=False)
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    code = main(
        ["--state", str(state_path), "--decision-out", str(out), "--gate", "release"]
    )
    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["decision"] == "deny"
    assert payload["reason_code"] == "stale_no_exception"


def test_exception_from_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ex = ws / "deploy/ops/snooze.json"
    ex.parent.mkdir(parents=True)
    ex.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "gate": "nonprod",
                "snooze": {
                    "expires_at": _future_iso(),
                    "approver": "bob",
                    "change_id": "CHG-9",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_WORKSPACE", str(ws))
    monkeypatch.setenv("NONPROD_ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", "deploy/ops/snooze.json")
    monkeypatch.delenv("NONPROD_ONCALL_EVIDENCE_EXCEPTION_JSON", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="nonprod",
        env_json_key="NONPROD_ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="NONPROD_ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
    )
    assert d == "allow"
    assert reason == "snooze_active"
    assert trace["change_id"] == "CHG-9"
    assert errs == []


def _policy_doc(**overrides: object) -> dict:
    base = {
        "schema_version": 1,
        "max_snooze_duration_hours": 168,
        "allowed_reason_codes": ["MAINTENANCE", "INCIDENT"],
        "snooze_required_fields": ["reason", "reason_code"],
        "gates": {
            "release": {
                "max_snooze_duration_hours": 72,
                "allowed_reason_codes": ["MAINTENANCE"],
            },
        },
    }
    base.update(overrides)
    return base


def test_evaluate_with_policy_requires_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pol = tmp_path / "pol.json"
    pol.write_text(json.dumps(_policy_doc()), encoding="utf-8")
    policy, perrs = _load_policy_file(pol)
    assert not perrs and policy is not None
    doc = {
        "schema_version": 1,
        "gate": "release",
        "snooze": {
            "expires_at": _future_iso(),
            "approver": "a@example.com",
            "change_id": "CHG-1",
            "reason": "x",
            "reason_code": "MAINTENANCE",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
        policy=policy,
    )
    assert d == "deny"
    assert reason == "exception_invalid_or_expired"
    assert trace is None
    assert any("environment" in e.lower() for e in errs)


def test_evaluate_with_policy_denies_snooze_over_max_hours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pol = tmp_path / "pol.json"
    pol.write_text(json.dumps(_policy_doc()), encoding="utf-8")
    policy, _ = _load_policy_file(pol)
    assert policy is not None
    t = datetime.now(timezone.utc) + timedelta(hours=100)
    expires = t.isoformat().replace("+00:00", "Z")
    doc = {
        "schema_version": 1,
        "gate": "release",
        "environment": "production",
        "snooze": {
            "expires_at": expires,
            "approver": "a@example.com",
            "change_id": "CHG-1",
            "reason": "x",
            "reason_code": "MAINTENANCE",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
        policy=policy,
    )
    assert d == "deny"
    assert trace is None
    assert any("exceeds policy" in e.lower() for e in errs)


def test_evaluate_with_policy_denies_disallowed_reason_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pol = tmp_path / "pol.json"
    pol.write_text(json.dumps(_policy_doc()), encoding="utf-8")
    policy, _ = _load_policy_file(pol)
    assert policy is not None
    doc = {
        "schema_version": 1,
        "gate": "release",
        "environment": "production",
        "snooze": {
            "expires_at": _future_iso(),
            "approver": "a@example.com",
            "change_id": "CHG-1",
            "reason": "x",
            "reason_code": "INCIDENT",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, _, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
        policy=policy,
    )
    assert d == "deny"
    assert trace is None
    assert any("reason_code" in e.lower() for e in errs)


def test_evaluate_example_policy_file_allows_compliant_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, perrs = _load_policy_file(POLICY_EXAMPLE)
    assert not perrs and policy is not None
    t = datetime.now(timezone.utc) + timedelta(hours=48)
    expires = t.isoformat().replace("+00:00", "Z")
    doc = {
        "schema_version": 1,
        "gate": "release",
        "environment": "production",
        "github_repository": "org/repo",
        "snooze": {
            "expires_at": expires,
            "approver": "a@example.com",
            "change_id": "CHG-1",
            "reason": "provider maintenance",
            "reason_code": "MAINTENANCE",
        },
    }
    monkeypatch.setenv("ONCALL_EVIDENCE_EXCEPTION_JSON", json.dumps(doc))
    monkeypatch.delenv("ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH", raising=False)
    d, reason, trace, errs = evaluate(
        _base_state(stale=True),
        gate="release",
        env_json_key="ONCALL_EVIDENCE_EXCEPTION_JSON",
        env_path_key="ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH",
        github_repository="org/repo",
        policy=policy,
    )
    assert d == "allow"
    assert reason == "snooze_active"
    assert trace is not None
    assert trace.get("policy_applied") is True
    assert errs == []


def test_main_policy_load_error_writes_decision(tmp_path: Path) -> None:
    bad_pol = tmp_path / "bad.json"
    bad_pol.write_text("{not json", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_base_state(stale=True)), encoding="utf-8")
    out = tmp_path / "decision.json"
    code = main(
        [
            "--state",
            str(state_path),
            "--decision-out",
            str(out),
            "--gate",
            "release",
            "--policy",
            str(bad_pol),
        ]
    )
    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["reason_code"] == "policy_load_error"
    assert payload["decision"] == "deny"


def test_main_approve_artifact_writes_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pol = tmp_path / "pol.json"
    pol.write_text(json.dumps(_policy_doc()), encoding="utf-8")
    t = datetime.now(timezone.utc) + timedelta(hours=24)
    expires = t.isoformat().replace("+00:00", "Z")
    ex = tmp_path / "ex.json"
    ex.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "gate": "release",
                "environment": "production",
                "github_repository": "org/repo",
                "snooze": {
                    "expires_at": expires,
                    "approver": "a@example.com",
                    "change_id": "CHG-9",
                    "reason": "maintenance",
                    "reason_code": "MAINTENANCE",
                },
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "approved.json"
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("GITHUB_ACTOR", "alice")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_SHA", "abc")
    code = main(
        [
            "--approve-artifact-out",
            str(out),
            "--exception-path",
            str(ex),
            "--policy",
            str(pol),
            "--approval-environment",
            "production",
        ]
    )
    assert code == 0
    art = json.loads(out.read_text(encoding="utf-8"))
    assert art["artifact_kind"] == "oncall_evidence_exception_approval"
    assert art["approval_environment"] == "production"
    assert art["approval_gate"] == "release"
    assert art["exception"]["snooze"]["change_id"] == "CHG-9"
    assert art["github_actor"] == "alice"
    assert "validation_trace" in art


def test_effective_policy_example_file_matches_release_gate() -> None:
    policy, errs = _load_policy_file(POLICY_EXAMPLE)
    assert not errs and policy is not None
    eff = _mod.effective_policy_for_gate(policy, "release")
    assert eff["max_snooze_duration_hours"] == 72
    assert eff["allowed_reason_codes"] == ["MAINTENANCE", "INCIDENT"]
