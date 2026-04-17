"""Contract checks for the on-call route verification GitHub workflow."""

from __future__ import annotations

from pathlib import Path


def test_oncall_route_verification_workflow_contract():
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "oncall-route-verification.yml"
    text = wf.read_text(encoding="utf-8")
    assert "name: On-call route verification" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "run_live_webhook_drill" in text
    assert "safe-verification:" in text
    assert "optional-live-webhook-drill:" in text
    assert "verify_oncall_route_policy.py" in text
    assert "deploy/ops/oncall-route-policy.example.json" in text
    assert "pytest tests/test_ops_alert_routing.py" in text
    assert "pytest tests/test_oncall_route_policy_verifier.py" in text
    assert "oncall-drill-evidence.jsonl" in text
    assert "release-readiness-summary.md" in text
    assert "oncall-incident-followup-checklist.md" in text
    assert "ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL" in text
    assert "ONCALL_DRILL_QUIET_UTC_START" in text
    assert "ONCALL_DRILL_QUIET_UTC_END" in text
    assert "ONCALL_VERIFICATION_ARTIFACT_RETENTION_DAYS" in text
    assert "artifact_retention" in text
    assert "retention-days:" in text
    assert "live_drill_gate" in text
    assert "actions/upload-artifact@v4" in text
    assert "oncall-release-readiness" in text
    assert "oncall-live-webhook-drill" in text
    assert "vars.ONCALL_SCHEDULED_LIVE_PROVIDER_DRILL == 'true'" in text
    # Live-drill operational closure docs + issue template hooks (stay wired in workflow artifacts/summaries).
    assert "docs/ops/runbooks/live_drill_staging.md" in text
    assert "docs/ops/runbooks/live_drill_prod.md" in text
    assert ".github/ISSUE_TEMPLATE/oncall-live-drill-followup.yml" in text
    assert "On-call live drill follow-up" in text
    assert "Ticket / issue automation hook" in text
    # Env-specific follow-up templates (markdown body + ticket JSON) in oncall-release-readiness.
    assert "oncall-live-drill-followup-staging.md" in text
    assert "oncall-live-drill-followup-production.md" in text
    assert "oncall-ticket-payload-staging.json" in text
    assert "oncall-ticket-payload-production.json" in text
    assert "ONCALL_TRACKER_STAGING_PROJECT" in text
    assert "ONCALL_TRACKER_PRODUCTION_PROJECT" in text
    assert "ONCALL_TRACKER_STAGING_LABELS" in text
    assert "ONCALL_TRACKER_PRODUCTION_LABELS" in text
    assert "GITHUB_REPOSITORY:" in text
    assert "GITHUB_SERVER_URL:" in text
    assert "oncall_live_drill_ticket_template/v1" in text
    assert "Env-specific templates" in text
    # Optional tracker API handoff (opt-in; downloads ticket JSON payloads from safe job artifact).
    assert "post_tracker_handoff" in text
    assert "optional-tracker-handoff:" in text
    assert "ONCALL_POST_TRACKER_HANDOFF" in text
    assert "secrets.ONCALL_TRACKER_STAGING_HANDOFF_URL" in text
    assert "secrets.ONCALL_TRACKER_PRODUCTION_HANDOFF_URL" in text
    assert "ONCALL_TRACKER_STAGING_HANDOFF_TOKEN" in text
    assert "oncall-tracker-handoff-results.json" in text
    assert "oncall_tracker_handoff_results/v2" in text
    assert "name: oncall-tracker-handoff" in text
    assert "actions/download-artifact@v4" in text
    assert "actions/checkout@v4" in text
    assert "validate_oncall_tracker_handoff.py" in text
    assert "deploy/ops/oncall_tracker_handoff_policy.example.json" in text
    assert "Tracker handoff governance (contract check, no network)" in text
    assert "tests/test_validate_oncall_tracker_handoff.py" in text
    assert "ONCALL_TRACKER_HANDOFF_POLICY_JSON" in text
    assert "ONCALL_TRACKER_HANDOFF_POLICY_PATH" in text
    assert "governance_audit" in text
    # Tracker handoff: provider adapters, idempotency, bounded retries (opt-in job only).
    assert "ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER" in text
    assert "ONCALL_TRACKER_PRODUCTION_HANDOFF_PROVIDER" in text
    assert "STAGING_PROVIDER:" in text
    assert "PRODUCTION_PROVIDER:" in text
    assert "validate_oncall_tracker_handoff.py run" in text
    assert "writeback-reconciliation" in text
    assert "ONCALL_TRACKER_HANDOFF_WRITEBACK_RECONCILIATION" in text
    assert "Idempotency-Key" in text
    assert "handoff_retry_policy" in text
    assert "policy_resolution" in text
    assert "policy_drift_vs_builtin_defaults" in text
    assert "closure_artifacts" in text
    assert "response.reconciliation" in text
    assert "builtin_defaults_reference" in text
    assert "Delivery closure" in text


def test_oncall_live_drill_closure_artifacts_exist():
    root = Path(__file__).resolve().parent.parent
    assert (root / "docs" / "ops" / "runbooks" / "live_drill_staging.md").is_file()
    assert (root / "docs" / "ops" / "runbooks" / "live_drill_prod.md").is_file()
    issue_tpl = root / ".github" / "ISSUE_TEMPLATE" / "oncall-live-drill-followup.yml"
    assert issue_tpl.is_file()
    body = issue_tpl.read_text(encoding="utf-8")
    assert "name: On-call live drill follow-up" in body
    assert "oncall-route-verification.yml" in body
    assert "oncall-incident-followup-checklist.md" in body
    assert "docs/ops/runbooks/live_drill_staging.md" in body
    assert "docs/ops/runbooks/live_drill_prod.md" in body
    assert "oncall-live-drill-followup.yml" in body
    assert "oncall-live-drill-followup-staging.md" in body
    assert "oncall-live-drill-followup-production.md" in body
    assert "oncall-ticket-payload-staging.json" in body
    assert "oncall-ticket-payload-production.json" in body
    assert "ONCALL_TRACKER_STAGING_PROJECT" in body
    cfg = root / ".github" / "ISSUE_TEMPLATE" / "config.yml"
    assert cfg.is_file()
    cfg_text = cfg.read_text(encoding="utf-8")
    assert "blank_issues_enabled" in cfg_text
    assert "oncall-live-drill-followup" in cfg_text


def test_live_drill_runbooks_reference_generated_followup_templates():
    root = Path(__file__).resolve().parent.parent
    staging = (root / "docs" / "ops" / "runbooks" / "live_drill_staging.md").read_text(
        encoding="utf-8"
    )
    prod = (root / "docs" / "ops" / "runbooks" / "live_drill_prod.md").read_text(encoding="utf-8")
    rollout = (root / "docs" / "ops" / "runbooks" / "production_observability_rollout.md").read_text(
        encoding="utf-8"
    )
    ops_readme = (root / "docs" / "ops" / "README.md").read_text(encoding="utf-8")
    for path, text in (
        ("live_drill_staging.md", staging),
        ("live_drill_prod.md", prod),
    ):
        assert "oncall-live-drill-followup-staging.md" in text, path
        assert "oncall-live-drill-followup-production.md" in text, path
        assert "oncall-ticket-payload-staging.json" in text, path
        assert "oncall-ticket-payload-production.json" in text, path
        assert "oncall-tracker-handoff" in text, path
        assert "post_tracker_handoff" in text, path
    assert "oncall-live-drill-followup-staging.md" in rollout
    assert "oncall-ticket-payload-production.json" in rollout
    assert "ONCALL_TRACKER_STAGING_PROJECT" in rollout
    assert "ONCALL_POST_TRACKER_HANDOFF" in rollout
    assert "oncall-tracker-handoff" in rollout
    assert "ONCALL_TRACKER_STAGING_HANDOFF_PROVIDER" in rollout
    assert "oncall-live-drill-followup-staging.md" in ops_readme
    assert "oncall-ticket-payload-staging.json" in ops_readme
    assert "oncall-tracker-handoff" in ops_readme
