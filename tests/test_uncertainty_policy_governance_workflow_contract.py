"""Contract checks for uncertainty policy governance verification workflow + policy metadata."""

from __future__ import annotations

import json
from pathlib import Path


def test_uncertainty_policy_governance_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "uncertainty-policy-governance-verification.yml"
    text = wf.read_text(encoding="utf-8")
    assert "Uncertainty policy governance verification" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "reusable-governance-attestation-check.yml" in text
    assert "baseline_verify" in text
    assert "secrets: inherit" in text
    assert "governance_environment_slug: uncertainty-governance-baseline-verification" in text
    reuse = root / ".github" / "workflows" / "reusable-governance-attestation-check.yml"
    rtext = reuse.read_text(encoding="utf-8")
    assert "environment: ${{ inputs.governance_environment_slug }}" in rtext
    assert "verify_uncertainty_policy_baselines.py" in rtext
    assert "UNCERTAINTY_GOVERNANCE_POLICY_BASELINES_JSON" in rtext
    assert "reject-example-baseline-path" in rtext
    assert "runner.temp" in rtext
    assert "uncertainty-governance-policy-baselines.private.json" in rtext
    assert "uncertainty-governance-policy-verification" in rtext
    assert "actions/upload-artifact@v4" in rtext
    assert "GITHUB_STEP_SUMMARY" in rtext
    assert "::error title=Uncertainty policy drift::" in rtext
    assert "continue-on-error: true" in rtext
    assert "if: always()" in rtext


def test_governance_policy_baseline_verification_metadata_contract():
    root = Path(__file__).resolve().parent.parent
    pol = root / "data" / "governed" / "uncertainty_governance_policy.json"
    data = json.loads(pol.read_text(encoding="utf-8"))
    g = data["ci"]["governance_baseline_verification"]
    assert g["example_baseline_path"] == "deploy/ops/uncertainty-governance-policy-baselines.example.json"
    assert g["verifier_script_path"] == "deploy/scripts/verify_uncertainty_policy_baselines.py"
    assert g["scheduled_workflow"] == ".github/workflows/uncertainty-policy-governance-verification.yml"
