"""Contract checks for the uncertainty promotion gate workflow (canary/prod)."""

from __future__ import annotations

from pathlib import Path


def test_uncertainty_promotion_gate_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "uncertainty-promotion-gate.yml"
    text = wf.read_text(encoding="utf-8")
    assert "Uncertainty promotion gate" in text
    assert "workflow_dispatch:" in text
    assert "promotion_stage:" in text
    assert "source_run_id:" in text
    assert "governance_acknowledged:" in text
    assert "promotion_approval_token:" in text
    assert "prod_baseline_profile_repo_path:" in text
    assert "policy_owning_team_attestation:" in text
    assert "policy_owner_attestor:" in text
    assert "policy_baseline_approval_change_id:" in text
    assert "policy_ownership_attestation_effective_date:" in text
    assert "ownership_attestation_max_age_days:" in text
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_MAX_AGE_DAYS" in text
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_REMINDER_DAYS_BEFORE_MAX" in text
    assert "UNCERTAINTY_POLICY_OWNING_TEAM" in text
    assert "environment: uncertainty-governance-${{ inputs.promotion_stage }}" in text
    assert "github_environment" in text
    assert "UNCERTAINTY_CANARY_PROMOTION_TOKEN" in text
    assert "UNCERTAINTY_PROD_PROMOTION_TOKEN" in text
    assert "UNCERTAINTY_GOVERNANCE_POLICY_SHA256" in text
    assert "gh run download" in text
    assert "uncertainty-candidate-profile" in text
    assert "uncertainty-profile-registry" in text
    assert "uncertainty-governance-audit-log" in text
    assert "uncertainty-profile promote" in text
    assert "--stage canary" in text
    assert "--stage prod" in text
    assert "--baseline-profile" in text
    assert "--governance-audit-log" in text
    assert "uncertainty_governance_audit.jsonl" in text
    assert "uncertainty-promotion-continuity-manifest" in text
    assert "promotion_continuity_manifest.json" in text
    assert "policy_ownership_attestation" in text
    assert "owning_team" in text
    assert "attestor_identity" in text
    assert "approval_change_id" in text
    assert "attestation_effective_date" in text
    assert "within_staleness_reminder_window" in text
    assert "actions: read" in text
    assert "COHERENCE_UNCERTAINTY_GOVERNANCE_HMAC_KEY" in text
    assert "report_governance_attestation_age.py" in text
    assert "validate-ownership-attestation" in text
    assert "governance-attestation-escalation" in text
    assert "governance_attestation_escalation.json" in text
    assert "route_governance_attestation_escalation.py" in text
    assert "Route governance attestation escalation" in text
    assert "GOVERNANCE_PROMOTION_ENV" in text
    assert "GOVERNANCE_GITHUB_ENVIRONMENT" in text
    assert "GOVERNANCE_PROMOTION_ENV: ${{ inputs.promotion_stage }}" in text
    assert "GOVERNANCE_ESCALATION_ROUTING_MAP_JSON" in text
    assert "governance-attestation-escalation-routing-map.example.json" in text
    assert "GOVERNANCE_ESCALATION_SINK: file" in text
