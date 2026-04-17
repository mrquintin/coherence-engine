"""Tests for aggregate_governance_attestation_reports.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_agg(
    repo_root: Path,
    inputs: list[Path],
    out_json: Path,
    out_md: Path | None = None,
    *,
    sla_policy: Path | None = None,
    sla_json: Path | None = None,
    sla_md: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(repo_root / "deploy" / "scripts" / "aggregate_governance_attestation_reports.py"),
    ]
    for p in inputs:
        cmd.extend(["--input", str(p)])
    cmd.extend(["--json-out", str(out_json)])
    if out_md is not None:
        cmd.extend(["--markdown-out", str(out_md)])
    if sla_policy is not None:
        cmd.extend(["--sla-policy", str(sla_policy)])
    if sla_json is not None:
        cmd.extend(["--sla-json-out", str(sla_json)])
    if sla_md is not None:
        cmd.extend(["--sla-markdown-out", str(sla_md)])
    return subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, check=False)


def test_aggregate_merges_latest_per_repository_and_counts_stale(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(
        json.dumps(
            {
                "ok": True,
                "report_kind": "governance_baseline_approval_age",
                "reported_at": "2026-04-01T00:00:00Z",
                "source_repository": "org/one",
                "environments": [
                    {"environment": "prod", "status": "stale", "age_days": 100, "last_baseline_approved_at": "2025-01-01"},
                    {"environment": "canary", "status": "ok", "age_days": 5, "last_baseline_approved_at": "2026-03-01"},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    b.write_text(
        json.dumps(
            {
                "ok": True,
                "report_kind": "governance_baseline_approval_age",
                "reported_at": "2026-04-02T00:00:00Z",
                "source_repository": "org/one",
                "environments": [
                    {"environment": "prod", "status": "ok", "age_days": 1, "last_baseline_approved_at": "2026-04-01"},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    c = tmp_path / "c.json"
    c.write_text(
        json.dumps(
            {
                "ok": True,
                "report_kind": "governance_baseline_approval_age",
                "reported_at": "2026-04-01T12:00:00Z",
                "source_repository": "org/two",
                "environments": [
                    {"environment": "prod", "status": "stale", "age_days": 200, "last_baseline_approved_at": "2025-01-01"},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    proc = _run_agg(root, [a, b, c], out_json, out_md)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["report_kind"] == "governance_attestation_trend_aggregate"
    assert data["inputs_used"] == 3
    sla = data["sla_summary"]
    # Latest for org/one is 2026-04-02 → prod ok, so stale only from org/two
    assert sla["stale_count_total"] == 1
    assert sla["stale_counts_by_repository"]["org/two"] == 1
    assert "org/one" in sla["stale_counts_by_repository"]
    assert sla["stale_counts_by_repository"]["org/one"] == 0
    assert "sla_policy_evaluation" not in data
    assert "governance attestation trend aggregate" in out_md.read_text(encoding="utf-8").lower()


def test_aggregate_skips_wrong_report_kind_with_warning(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    good.write_text(
        json.dumps(
            {
                "ok": True,
                "report_kind": "governance_baseline_approval_age",
                "reported_at": "2026-04-01T00:00:00Z",
                "source_repository": "org/x",
                "environments": [{"environment": "prod", "status": "ok", "age_days": 1}],
            }
        ),
        encoding="utf-8",
    )
    bad.write_text(json.dumps({"report_kind": "other", "ok": True}), encoding="utf-8")
    out_json = tmp_path / "out.json"
    proc = _run_agg(root, [good, bad], out_json)
    assert proc.returncode == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["inputs_used"] == 1
    assert any("skip" in w.lower() for w in (data.get("warnings") or []))


def test_aggregate_fails_on_missing_file(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    out_json = tmp_path / "out.json"
    proc = _run_agg(root, [tmp_path / "nope.json"], out_json)
    assert proc.returncode == 1


def test_aggregate_sla_policy_emits_evaluation_artifacts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    report = tmp_path / "r.json"
    report.write_text(
        json.dumps(
            {
                "ok": True,
                "report_kind": "governance_baseline_approval_age",
                "reported_at": "2026-04-01T00:00:00Z",
                "source_repository": "org/acme",
                "environments": [
                    {"environment": "prod", "status": "reminder", "age_days": 10},
                    {"environment": "canary", "status": "ok", "age_days": 5},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "defaults": {"allowed_statuses": ["ok"], "max_age_days": 90},
                "environments": {"prod": {"allowed_statuses": ["ok"], "max_age_days": 8}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    out_json = tmp_path / "agg.json"
    out_md = tmp_path / "agg.md"
    sla_json = tmp_path / "sla.json"
    sla_md = tmp_path / "sla.md"
    proc = _run_agg(
        root,
        [report],
        out_json,
        out_md,
        sla_policy=policy,
        sla_json=sla_json,
        sla_md=sla_md,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(out_json.read_text(encoding="utf-8"))
    ev = data.get("sla_policy_evaluation")
    assert isinstance(ev, dict)
    assert ev["report_kind"] == "governance_attestation_sla_evaluation"
    assert ev["compliant"] is False
    assert ev["summary"]["breach_count"] == 1
    assert len(ev["breaches"]) == 1
    b0 = ev["breaches"][0]
    assert b0["environment"] == "prod"
    assert set(b0["breach_reasons"]) == {"age_exceeds_max", "status_not_allowed"}
    standalone = json.loads(sla_json.read_text(encoding="utf-8"))
    assert standalone["summary"]["breach_count"] == ev["summary"]["breach_count"]
    assert "governance attestation sla evaluation" in sla_md.read_text(encoding="utf-8").lower()
    assert "governance attestation sla evaluation" in out_md.read_text(encoding="utf-8").lower()


def test_aggregate_sla_policy_skips_row_with_no_applicable_rule(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent.parent
    report = tmp_path / "r.json"
    report.write_text(
        json.dumps(
            {
                "ok": True,
                "report_kind": "governance_baseline_approval_age",
                "reported_at": "2026-04-01T00:00:00Z",
                "source_repository": "org/acme",
                "environments": [{"environment": "edge", "status": "stale", "age_days": 999}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema_version": 1, "environments": {}}), encoding="utf-8")
    out_json = tmp_path / "agg.json"
    proc = _run_agg(root, [report], out_json, sla_policy=policy)
    assert proc.returncode == 0
    ev = json.loads(out_json.read_text(encoding="utf-8"))["sla_policy_evaluation"]
    assert ev["compliant"] is True
    assert ev["summary"]["skipped_environment_rows_no_applicable_rule"] == 1
    assert ev["summary"]["evaluated_environment_rows"] == 0
