"""Lightweight contract checks for the uncertainty recalibration GitHub workflow."""

from __future__ import annotations

import json
from pathlib import Path


def test_uncertainty_recalibration_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "uncertainty-recalibration.yml"
    text = wf.read_text(encoding="utf-8")
    assert "Uncertainty recalibration" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "promote_to_shadow" in text
    assert "promote_to_canary" in text
    assert "promote_to_prod" in text
    assert "prod_baseline_profile_repo_path" in text
    assert "policy_owning_team_attestation" in text
    assert "policy_owner_attestor" in text
    assert "policy_baseline_approval_change_id" in text
    assert "policy_ownership_attestation_effective_date" in text
    assert "ownership_attestation_max_age_days" in text
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_MAX_AGE_DAYS" in text
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_REMINDER_DAYS_BEFORE_MAX" in text
    assert "Ownership attestation rotation policy" in text
    assert "UNCERTAINTY_POLICY_OWNING_TEAM" in text
    assert "uncertainty-governance-canary" in text
    assert "uncertainty-governance-prod" in text
    assert "uncertainty-governance-recalibration" in text
    assert "github_environment" in text
    assert "UNCERTAINTY_CANARY_PROMOTION_TOKEN" in text
    assert "UNCERTAINTY_PROD_PROMOTION_TOKEN" in text
    assert "UNCERTAINTY_GOVERNANCE_POLICY_SHA256" in text
    assert "uncertainty-profile verify-dataset" in text
    assert "validate-historical-export" in text
    assert "uncertainty-historical-outcomes-export.example.json" in text
    assert "require-standard-layer-keys" in text
    assert "data/governed/uncertainty_historical_outcomes.jsonl" in text
    assert "uncertainty_historical_outcomes.manifest.json" in text
    assert "calibrate-uncertainty" in text
    assert "uncertainty-profile promote" in text
    assert "--governance-policy" in text
    assert "data/governed/uncertainty_governance_policy.json" in text
    assert "--governance-audit-log" in text
    assert "uncertainty_governance_audit.jsonl" in text
    assert "uncertainty-governance-audit-log" in text
    assert "uncertainty-promotion-continuity-manifest" in text
    assert "promotion_continuity_manifest.json" in text
    assert "attestation_effective_date" in text
    assert "uncertainty-candidate-profile" in text
    assert "actions/upload-artifact@v4" in text
    assert "rollback-policy-eval" in text
    assert "calibration_health_evidence.json" in text
    assert "uncertainty-rollback-policy-eval" in text
    assert "UNCERTAINTY_ROLLBACK_MIN_COVERAGE" in text
    assert "COHERENCE_UNCERTAINTY_GOVERNANCE_HMAC_KEY" in text
    assert "UNCERTAINTY_GOVERNANCE_HMAC_KEY" in text
    assert "Choose at most one" in text
    assert "report_governance_attestation_age.py" in text
    assert "validate-ownership-attestation" in text
    assert "governance-attestation-escalation" in text
    assert "governance_attestation_escalation.json" in text
    assert "route_governance_attestation_escalation.py" in text
    assert "Route governance attestation escalation" in text
    assert "GOVERNANCE_PROMOTION_ENV" in text
    assert "GOVERNANCE_GITHUB_ENVIRONMENT" in text
    assert "inputs.promote_to_canary == true && 'canary'" in text
    assert "GOVERNANCE_ESCALATION_ROUTING_MAP_JSON" in text
    assert "governance-attestation-escalation-routing-map.example.json" in text
    assert "GOVERNANCE_ESCALATION_SINK: file" in text


def test_governance_policy_ci_pinning_contract():
    root = Path(__file__).resolve().parent.parent
    pol = root / "data" / "governed" / "uncertainty_governance_policy.json"
    data = json.loads(pol.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    pinning = data["ci"]["pinning"]
    assert pinning["github_repository_variable_sha256"] == "UNCERTAINTY_GOVERNANCE_POLICY_SHA256"
    assert pinning["approval_secrets_by_stage"]["shadow"] == "UNCERTAINTY_SHADOW_PROMOTION_TOKEN"
    assert pinning["approval_secrets_by_stage"]["canary"] == "UNCERTAINTY_CANARY_PROMOTION_TOKEN"
    assert pinning["approval_secrets_by_stage"]["prod"] == "UNCERTAINTY_PROD_PROMOTION_TOKEN"
