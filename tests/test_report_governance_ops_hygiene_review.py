"""Tests for deploy/scripts/report_governance_ops_hygiene_review.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "scripts" / "report_governance_ops_hygiene_review.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def test_report_all_skipped_when_no_inputs(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    md = tmp_path / "r.md"
    proc = _run("--json-out", str(out), "--markdown-out", str(md))
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["report_kind"] == "governance_ops_hygiene_review"
    assert doc["sections"]["enrollment_coverage"]["skipped"] is True
    assert "Governance / on-call ops hygiene review" in md.read_text(encoding="utf-8")


def test_report_summarizes_optional_artifacts(tmp_path: Path) -> None:
    enroll = tmp_path / "e.json"
    enroll.write_text(
        json.dumps(
            {
                "report_kind": "governance_enrollment_coverage",
                "compliant": False,
                "summary": {"expected_count": 2, "observed_count": 1, "missing_count": 1},
                "manifest_errors": ["x"],
            }
        ),
        encoding="utf-8",
    )
    wh = tmp_path / "w.json"
    wh.write_text(
        json.dumps(
            {
                "record_type": "governance_escalation_webhook_delivery_receipt",
                "hmac_key_configured": True,
                "live_post_requested": True,
                "force_dry_run": False,
                "channels": [
                    {"delivery": {"mode": "dry_run"}},
                    {"delivery": {"mode": "skipped_no_url"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    hg = tmp_path / "h.json"
    hg.write_text(
        json.dumps(
            {
                "report_kind": "oncall_tracker_handoff_governance_summary",
                "policy": {"source": "secret_json", "drift_any_environment": True},
                "status_by_environment": {"staging": "success"},
                "reconciliation_coverage": {"staging": {"applicable": True}},
            }
        ),
        encoding="utf-8",
    )
    rp = tmp_path / "p.json"
    rp.write_text(
        json.dumps(
            {
                "record_type": "governance_escalation_routing_proof",
                "channels": [
                    {"routing": {"webhook_url_configured": False}},
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    proc = _run(
        "--enrollment-json",
        str(enroll),
        "--webhook-receipt-json",
        str(wh),
        "--handoff-governance-json",
        str(hg),
        "--routing-proof-json",
        str(rp),
        "--json-out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["sections"]["enrollment_coverage"]["missing_count"] == 1
    assert doc["sections"]["escalation_webhook_delivery_receipt"]["channel_count"] == 2
    assert doc["sections"]["tracker_handoff_governance"]["policy_drift_any"] is True
    assert doc["sections"]["escalation_routing_proof"]["channel_count"] == 1
    assert any("Enrollment coverage" in r for r in doc["reminders"])
    assert any("live POST" in r for r in doc["reminders"])
    assert any("drift" in r.lower() for r in doc["reminders"])


def test_report_invalid_json_path_records_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    out = tmp_path / "out.json"
    proc = _run("--enrollment-json", str(bad), "--json-out", str(out))
    assert proc.returncode == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert "enrollment" in doc["input_errors"]
