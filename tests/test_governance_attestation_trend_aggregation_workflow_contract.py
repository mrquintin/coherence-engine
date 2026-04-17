"""Contract checks for governance attestation trend aggregation workflow."""

from __future__ import annotations

from pathlib import Path


def test_governance_attestation_trend_aggregation_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "governance-attestation-trend-aggregation.yml"
    text = wf.read_text(encoding="utf-8")
    assert "Governance attestation trend aggregation" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "enable_cross_repo_collection" in text
    assert "governance_environment" in text
    assert "environment:" in text
    assert "governance-attestation-trends" in text
    assert "GOVERNANCE_ATTESTATION_TREND_ENVIRONMENT" in text
    assert "UNCERTAINTY_GOVERNANCE_POLICY_BASELINES_JSON" in text
    assert "uncertainty-governance-policy-baselines.example.json" in text
    assert "workflow_dispatch testing mode" in text
    assert "Validate scheduled SLA" in text
    assert "GOVERNANCE_ATTESTATION_TREND_CROSS_REPO_ENABLED" in text
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_MAX_AGE_DAYS" in text
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_REMINDER_DAYS_BEFORE_MAX" in text
    assert "aggregate_governance_attestation_reports.py" in text
    assert "governance-attestation-trend-aggregate.json" in text
    assert "governance_attestation_trend_dashboard.md" in text
    assert "governance-attestation-trend-aggregate" in text
    assert "report_governance_attestation_age.py report" in text
    assert "GOVERNANCE_ATTESTATION_TREND_GH_TOKEN" in text
    assert "GOVERNANCE_ATTESTATION_TREND_REPO_LIST" in text
    assert "GOVERNANCE_ATTESTATION_SLA_POLICY" in text
    assert "GOVERNANCE_ATTESTATION_SLA_POLICY_PATH" in text
    assert "sla_policy_repo_path" in text
    assert "governance-attestation-sla-evaluation.json" in text
    assert "governance-attestation-sla-evaluation.md" in text
    assert "--sla-policy" in text
    assert "report_governance_enrollment_coverage.py" in text
    assert "governance-enrollment-coverage.json" in text
    assert "governance_enrollment_coverage.md" in text
    assert "GOVERNANCE_ENROLLMENT_EXPECTED_REPO_LIST" in text
    assert "GOVERNANCE_ENROLLMENT_SLA_EXCEPTION_MANIFEST_JSON" in text
    assert "GOVERNANCE_ENROLLMENT_FAIL_ON_MISSING" in text
    assert "enrollment-sla-exception-manifest.json" in text
    assert "actions/upload-artifact@v4" in text
    assert "actions/checkout@v4" in text
