"""Tests for uncertainty calibration governance (gates, signing, rollback policy)."""

from __future__ import annotations

import json

import pytest

from coherence_engine.server.fund.services.uncertainty_governance import (
    GOVERNANCE_HMAC_ENV,
    GateThresholds,
    GateEvaluation,
    GovernanceError,
    RollbackDecision,
    RollbackPolicy,
    append_audit_jsonl,
    build_promotion_audit_record,
    build_rollback_audit_record,
    canonical_signing_bytes,
    evaluate_quality_gates,
    evaluate_rollback_trigger,
    extract_calibration_metrics,
    gate_thresholds_any_set,
    gate_thresholds_from_mapping,
    governance_hmac_key_bytes,
    load_metrics_json,
    load_uncertainty_governance_policy,
    merge_gate_thresholds,
    merge_rollback_policy,
    normalize_health_metrics,
    rollback_policy_any_set,
    sign_audit_record,
    verify_audit_record,
)


def _sample_calibration(*, cov: float = 0.96, mw: float = 0.1, n_used: int = 50) -> dict:
    return {
        "uncertainty_model_version": "x",
        "n_records_used": n_used,
        "metrics": {
            "coverage": cov,
            "mean_width": mw,
            "mean_absolute_error": 0.01,
            "n_evaluated": float(n_used),
        },
    }


def test_extract_calibration_metrics_ok():
    m = extract_calibration_metrics(_sample_calibration())
    assert m["coverage"] == pytest.approx(0.96)
    assert m["mean_width"] == pytest.approx(0.1)
    assert int(m["record_count"]) == 50


def test_extract_calibration_metrics_rejects_missing():
    with pytest.raises(GovernanceError, match="metrics"):
        extract_calibration_metrics({})


def test_gates_pass_and_fail_coverage():
    cal = _sample_calibration(cov=0.99)
    ok = evaluate_quality_gates(cal, GateThresholds(min_coverage=0.95))
    assert ok.approved
    bad = evaluate_quality_gates(cal, GateThresholds(min_coverage=1.0))
    assert not bad.approved
    assert bad.failures


def test_gates_mean_width_and_record_count():
    cal = _sample_calibration(mw=0.2, n_used=10)
    g = evaluate_quality_gates(
        cal,
        GateThresholds(max_mean_width=0.15, min_record_count=20),
    )
    assert not g.approved
    assert len(g.failures) == 2


def test_gates_baseline_deltas():
    cand = _sample_calibration(cov=0.90, mw=0.12)
    base = _sample_calibration(cov=0.95, mw=0.10)
    g = evaluate_quality_gates(
        cand,
        GateThresholds(
            max_coverage_drop_vs_baseline=0.03,
            max_mean_width_increase_vs_baseline=0.01,
        ),
        baseline_calibration=base,
    )
    assert not g.approved
    assert g.baseline_metrics is not None


def test_sign_unsigned_no_secret(monkeypatch):
    monkeypatch.delenv(GOVERNANCE_HMAC_ENV, raising=False)
    assert governance_hmac_key_bytes() is None
    r = sign_audit_record({"a": 1, "b": "x"})
    assert r["signing_mode"] == "unsigned_no_secret"
    assert r["signature"] is None
    assert verify_audit_record(r)


def test_sign_hmac_roundtrip(monkeypatch):
    monkeypatch.setenv(GOVERNANCE_HMAC_ENV, "test-secret-key")
    r = sign_audit_record({"op": "promote", "stage": "shadow"})
    assert r["signing_mode"] == "hmac_sha256"
    assert r["signature"] and len(r["signature"]) == 64
    assert verify_audit_record(r)


def test_verify_fails_wrong_key(monkeypatch):
    monkeypatch.setenv(GOVERNANCE_HMAC_ENV, "a")
    r = sign_audit_record({"k": 1})
    monkeypatch.setenv(GOVERNANCE_HMAC_ENV, "b")
    assert not verify_audit_record(r)


def test_canonical_signing_bytes_deterministic():
    a = sign_audit_record({"z": 1, "a": 2})
    b = canonical_signing_bytes(a)
    c = canonical_signing_bytes(a)
    assert b == c


def test_append_audit_jsonl(tmp_path):
    p = tmp_path / "audit.jsonl"
    r1 = sign_audit_record({"n": 1})
    r2 = sign_audit_record({"n": 2})
    append_audit_jsonl(p, r1)
    append_audit_jsonl(p, r2)
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["n"] == 1


def test_build_promotion_audit_record_signed(monkeypatch, tmp_path):
    monkeypatch.setenv(GOVERNANCE_HMAC_ENV, "k")
    prof = tmp_path / "c.json"
    prof.write_text("{}", encoding="utf-8")
    ge = GateEvaluation(
        approved=True,
        metrics={"coverage": 1.0, "mean_width": 0.0, "record_count": 1.0},
        failures=(),
    )
    rec = build_promotion_audit_record(
        operation="promote",
        stage="canary",
        registry_path=str(tmp_path / "r.json"),
        profile_path=str(prof),
        profile_sha256="abc",
        gate_evaluation=ge,
        forced=False,
        reason="ci",
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert rec["operation"] == "promote"
    assert rec["forced"] is False
    assert verify_audit_record(rec)


def test_rollback_policy_eval_triggers():
    m = {"coverage": 0.5, "mean_width": 0.9, "n_records_used": 3}
    d = evaluate_rollback_trigger(
        m,
        RollbackPolicy(min_coverage=0.9, max_mean_width=0.5, min_record_count=10),
    )
    assert d.should_rollback
    assert len(d.reasons) == 3


def test_rollback_policy_no_trigger():
    m = _sample_calibration()
    d = evaluate_rollback_trigger(
        m,
        RollbackPolicy(min_coverage=0.5, max_mean_width=1.0, min_record_count=1),
    )
    assert not d.should_rollback


def test_normalize_health_metrics_nested():
    raw = {"metrics": {"coverage": 0.9, "mean_width": 0.1}, "n_records_used": 5}
    n = normalize_health_metrics(raw)
    assert n["coverage"] == pytest.approx(0.9)
    assert int(n["record_count"]) == 5


def test_load_metrics_json_roundtrip(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"coverage": 1.0, "mean_width": 0.0, "record_count": 1}), encoding="utf-8")
    assert load_metrics_json(p)["coverage"] == 1.0


def test_build_rollback_audit_record(monkeypatch, tmp_path):
    monkeypatch.delenv(GOVERNANCE_HMAC_ENV, raising=False)
    rec = build_rollback_audit_record(
        stage="prod",
        registry_path=str(tmp_path / "r.json"),
        policy_decision=RollbackDecision(True, ("x",)),
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert rec["operation"] == "rollback"
    assert rec["rollback_trigger_recommended"] is True
    assert verify_audit_record(rec)


def test_load_uncertainty_governance_policy_repo_file():
    doc = load_uncertainty_governance_policy()
    assert doc.schema_version == 1
    assert doc.source_path.endswith("uncertainty_governance_policy.json")
    shadow = doc.promotion_gate_thresholds("shadow")
    assert gate_thresholds_any_set(shadow)
    assert shadow.min_coverage == pytest.approx(0.85)
    prod = doc.promotion_gate_thresholds("prod")
    assert prod.max_coverage_drop_vs_baseline == pytest.approx(0.02)


def test_merge_gate_thresholds_cli_overrides_policy():
    base = GateThresholds(min_coverage=0.9, max_mean_width=0.5)
    override = GateThresholds(min_coverage=0.95)
    m = merge_gate_thresholds(base, override)
    assert m.min_coverage == pytest.approx(0.95)
    assert m.max_mean_width == pytest.approx(0.5)


def test_policy_rejects_invalid_schema(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"schema_version": 2, "stages": {}}', encoding="utf-8")
    with pytest.raises(GovernanceError, match="schema_version"):
        load_uncertainty_governance_policy(p)


def test_policy_requires_all_stages(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stages": {
                    "shadow": {"promotion_gates": {"min_coverage": 0.8}},
                    "canary": {"promotion_gates": {}},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GovernanceError, match="stages.prod"):
        load_uncertainty_governance_policy(p)


def test_gate_thresholds_from_mapping_coerces_int():
    t = gate_thresholds_from_mapping({"min_record_count": 10, "min_coverage": 1})
    assert t.min_record_count == 10
    assert t.min_coverage == pytest.approx(1.0)


def test_merge_rollback_policy():
    base = RollbackPolicy(min_coverage=0.9)
    over = RollbackPolicy(max_mean_width=0.3)
    m = merge_rollback_policy(base, over)
    assert m.min_coverage == pytest.approx(0.9)
    assert m.max_mean_width == pytest.approx(0.3)


def test_rollback_policy_any_set_false():
    assert not rollback_policy_any_set(RollbackPolicy())


def test_build_promotion_audit_record_includes_policy_fields(monkeypatch, tmp_path):
    monkeypatch.delenv(GOVERNANCE_HMAC_ENV, raising=False)
    prof = tmp_path / "c.json"
    prof.write_text("{}", encoding="utf-8")
    ge = GateEvaluation(
        approved=True,
        metrics={"coverage": 1.0, "mean_width": 0.0, "record_count": 1.0},
        failures=(),
    )
    pol_path = tmp_path / "pol.json"
    pol_path.write_text("{}", encoding="utf-8")
    rec = build_promotion_audit_record(
        operation="promote",
        stage="shadow",
        registry_path=str(tmp_path / "r.json"),
        profile_path=str(prof),
        profile_sha256="abc",
        gate_evaluation=ge,
        forced=False,
        reason="ci",
        recorded_at="2026-01-01T00:00:00Z",
        governance_policy_path=str(pol_path),
        governance_policy_schema_version=1,
    )
    assert rec["governance_policy_path"] == str(pol_path.resolve())
    assert rec["governance_policy_schema_version"] == 1
