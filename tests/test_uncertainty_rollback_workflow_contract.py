"""Lightweight contract checks for the uncertainty rollback policy verification workflow."""

from __future__ import annotations

from pathlib import Path


def test_uncertainty_rollback_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "uncertainty-rollback-verification.yml"
    text = wf.read_text(encoding="utf-8")
    assert "Uncertainty rollback policy verification" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "rollback-policy-eval" in text
    assert "artifacts/examples/calibration_health_example.json" in text
    assert "uncertainty-rollback-policy-report" in text
    assert "actions/upload-artifact@v4" in text
    assert "METRICS_INPUT" in text
    assert "rollback_policy_decision.json" in text
    assert "rollback_policy_summary.md" in text


def test_calibration_health_example_artifact_exists():
    root = Path(__file__).resolve().parent.parent
    p = root / "artifacts" / "examples" / "calibration_health_example.json"
    assert p.is_file()
