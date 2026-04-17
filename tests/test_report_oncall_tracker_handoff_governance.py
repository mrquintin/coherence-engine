"""Tests for deploy/scripts/report_oncall_tracker_handoff_governance.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "scripts" / "report_oncall_tracker_handoff_governance.py"


def test_report_summarizes_handoff(tmp_path: Path) -> None:
    handoff = tmp_path / "oncall-tracker-handoff-results.json"
    doc = {
        "schema": "oncall_tracker_handoff_results/v2",
        "github_repository": "o/r",
        "github_run_id": "9",
        "trigger_detail": "test",
        "ts_iso": "2026-04-01T00:00:00Z",
        "summary": "ok",
        "governance_audit": {
            "policy_source": "built_ins",
            "policy_resolution": {"precedence_order": []},
            "policy_drift_vs_builtin_defaults": {
                "staging": {"has_drift": False},
                "production": {"has_drift": False},
            },
        },
        "environments": [
            {
                "environment": "staging",
                "status": "success",
                "response": {
                    "reconciliation": {
                        "applicable": True,
                        "tracker_issue_key": "STG-1",
                    }
                },
            },
            {
                "environment": "production",
                "status": "skipped_no_url",
                "response": {"reconciliation": {"applicable": False, "skip_reason": "no_url"}},
            },
        ],
    }
    handoff.write_text(json.dumps(doc), encoding="utf-8")
    out_json = tmp_path / "gov.json"
    out_md = tmp_path / "gov.md"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--handoff-json",
            str(handoff),
            "--json-out",
            str(out_json),
            "--markdown-out",
            str(out_md),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    rep = json.loads(out_json.read_text(encoding="utf-8"))
    assert rep["report_kind"] == "oncall_tracker_handoff_governance_summary"
    assert rep["reconciliation_coverage"]["staging"]["applicable"] is True
    assert "On-call tracker handoff governance summary" in out_md.read_text(encoding="utf-8")


def test_oncall_tracker_handoff_governance_report_workflow_contract() -> None:
    root = Path(__file__).resolve().parent.parent
    wf = root / ".github" / "workflows" / "oncall-tracker-handoff-governance-report.yml"
    text = wf.read_text(encoding="utf-8")
    assert "On-call tracker handoff governance report" in text
    assert "oncall-route-verification.yml" in text
    assert "report_oncall_tracker_handoff_governance.py" in text
    assert "oncall-tracker-handoff-governance-report" in text
    assert "oncall-tracker-handoff-governance.json" in text
    assert "actions: read" in text
