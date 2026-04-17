"""Contract checks for on-call evidence exception approval workflow."""

from __future__ import annotations

from pathlib import Path


def test_oncall_evidence_exception_approval_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "oncall-evidence-exception-approval.yml"
    text = wf.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "environment:" in text
    assert "type: choice" in text
    assert "production" in text
    assert "nonprod" in text
    assert "exception_json:" in text
    assert "policy_relative_path:" in text
    assert "oncall-evidence-exception-approval-${{ inputs.environment }}" in text
    assert "evaluate_oncall_evidence_exception.py" in text
    assert "--approve-artifact-out" in text
    assert "--approval-environment" in text
    assert "oncall-evidence-exception-approved.json" in text
    assert "actions/upload-artifact@v4" in text
    assert "oncall-evidence-exception-approved-" in text
    assert "contents: read" in text
