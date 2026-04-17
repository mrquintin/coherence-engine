"""Contract checks for release-related GitHub workflows and on-call gating."""

from __future__ import annotations

from pathlib import Path


def test_release_workflow_oncall_gate_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "release.yml"
    text = wf.read_text(encoding="utf-8")
    assert "oncall_release_gate:" in text
    assert "name: On-call policy & drill gate" in text
    assert "Resolve private on-call route policy" in text
    assert "secrets.ONCALL_ROUTE_POLICY_JSON" in text
    assert "vars.ONCALL_ROUTE_POLICY_RELATIVE_PATH" in text
    assert "oncall-route-policy.private.json" in text
    assert "deploy/ops/oncall-route-policy.example.json" not in text
    assert "verify_oncall_route_policy.py" in text
    assert "--policy artifacts/oncall/oncall-route-policy.private.json" in text
    assert "--fail-on-stale-escalation-ownership" in text
    assert "--fail-on-stale-oncall-route-policy" in text
    assert "--fail-on-stale-verification-evidence" in text
    assert "--require-escalation-rotation-ref" in text
    assert "--fail-on-stale-oncall-route-policy" in text
    assert "--fail-on-stale-verification-evidence" in text
    assert "pytest tests/test_ops_alert_routing.py" in text
    assert "pytest tests/test_oncall_route_policy_verifier.py" in text
    assert "oncall-drill-evidence.jsonl" in text
    assert "release-readiness-summary.md" in text
    assert "needs: [preflight_gate, oncall_release_gate]" in text
    assert "needs.oncall_release_gate.result == 'success'" in text
    assert "actions/upload-artifact@v4" in text
    assert "oncall-release-gate-" in text
    assert "Release on-call gate requires environment-owned private policy" in text
    assert "actions: read" in text
    assert "Live-drill evidence freshness (GitHub Actions metadata)" in text
    assert "oncall-route-verification.yml" in text
    assert "oncall-live-webhook-drill" in text
    assert "vars.ONCALL_LIVE_DRILL_EVIDENCE_MAX_AGE_HOURS" in text
    assert "vars.ONCALL_LIVE_DRILL_RUNS_BRANCH" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "subprocess.run" in text or "gh" in text
    assert "ONCALL_LIVE_DRILL_EVIDENCE_MAX_AGE_HOURS" in text
    assert "live-drill-evidence-state.json" in text
    assert "evaluate_oncall_evidence_exception.py" in text
    assert "oncall-evidence-gate-decision.json" in text
    assert "secrets.ONCALL_EVIDENCE_EXCEPTION_JSON" in text
    assert "vars.ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH" in text
    assert "--gate release" in text
    assert "oncall-evidence-gate-decision-release-" in text
    assert "Upload on-call evidence gate decision (always)" in text
    assert "oncall_evidence_gate_decision" in text
    # No scheduled-workflow artifact coupling for this gate
    assert "actions/download-artifact" not in text
    assert "workflow_run:" not in text
