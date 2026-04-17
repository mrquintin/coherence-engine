"""Contract checks for the uncertainty attestation lifecycle (scheduled aging) workflow."""

from __future__ import annotations

from pathlib import Path


def test_uncertainty_attestation_lifecycle_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "uncertainty-attestation-lifecycle.yml"
    text = wf.read_text(encoding="utf-8")
    assert "Uncertainty attestation lifecycle" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "uncertainty-governance-attestation-lifecycle" in text
    assert "reusable-governance-attestation-check.yml" in text
    assert "attestation_aging" in text
    assert "secrets: inherit" in text
    reuse = root / ".github" / "workflows" / "reusable-governance-attestation-check.yml"
    rtext = reuse.read_text(encoding="utf-8")
    assert "report_governance_attestation_age.py report" in rtext
    assert "governance-attestation-aging-report.json" in rtext
    assert "governance_attestation_reminder_summary.md" in rtext
    assert "governance-attestation-aging-report" in rtext
    assert "UNCERTAINTY_GOVERNANCE_POLICY_BASELINES_JSON" in rtext
    assert "uncertainty-governance-policy-baselines.example.json" in rtext
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_MAX_AGE_DAYS" in rtext
    assert "UNCERTAINTY_OWNERSHIP_ATTESTATION_REMINDER_DAYS_BEFORE_MAX" in rtext
    assert "actions/upload-artifact@v4" in rtext
    assert "actions/checkout@v4" in rtext
    assert "governance-escalation-routing-proof" in text
    assert "emit-routing-proof" in text
    assert "governance_escalation_routing_proof.json" in text
    assert "governance-attestation-escalation-routing-map.example.json" in text
    assert "GOVERNANCE_ESCALATION_ROUTING_MAP_JSON" in text
    assert "governance-escalation-webhook-delivery-receipt" in text
    assert "emit-webhook-delivery-receipt" in text
    assert "governance_escalation_webhook_delivery_receipt.json" in text
    assert "governance_webhook_verify_live_post" in text
    assert "GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY" in text
