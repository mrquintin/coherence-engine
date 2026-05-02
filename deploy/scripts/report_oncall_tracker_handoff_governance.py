#!/usr/bin/env python3
"""Summarize oncall-tracker-handoff-results.json for release-readiness (local files only).

Aggregates policy source, drift flags, per-environment status, and reconciliation coverage.
No network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

RESULT_SCHEMA = "oncall_tracker_handoff_results/v2"
REPORT_KIND = "oncall_tracker_handoff_governance_summary"


def _load_handoff(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("handoff results root must be object")
    if doc.get("schema") != RESULT_SCHEMA:
        raise ValueError(f"expected schema {RESULT_SCHEMA!r}, got {doc.get('schema')!r}")
    return doc


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    gov = doc.get("governance_audit") or {}
    drift = gov.get("policy_drift_vs_builtin_defaults") or {}
    env_rows = doc.get("environments") if isinstance(doc.get("environments"), list) else []

    recon_stats: dict[str, Any] = {
        "staging": {"attempted": False, "applicable": False, "has_key_or_url_hint": False},
        "production": {"attempted": False, "applicable": False, "has_key_or_url_hint": False},
    }

    status_by_env: dict[str, str] = {}
    for row in env_rows:
        if not isinstance(row, dict):
            continue
        env = str(row.get("environment", "")).strip().lower()
        if env not in recon_stats:
            continue
        status_by_env[env] = str(row.get("status", ""))
        resp = row.get("response") if isinstance(row.get("response"), dict) else {}
        recon = resp.get("reconciliation") if isinstance(resp.get("reconciliation"), dict) else {}
        st = row.get("status")
        if st in ("success", "failed"):
            recon_stats[env]["attempted"] = True
        if recon.get("applicable") is True:
            recon_stats[env]["applicable"] = True
        if any(
            recon.get(k)
            for k in ("tracker_issue_key", "tracker_issue_number", "tracker_resource_hint")
        ):
            recon_stats[env]["has_key_or_url_hint"] = True

    drift_any = False
    if isinstance(drift, dict):
        for _k, v in drift.items():
            if isinstance(v, dict) and v.get("has_drift") is True:
                drift_any = True
                break

    return {
        "schema_version": 1,
        "report_kind": REPORT_KIND,
        "source_handoff_schema": doc.get("schema"),
        "github_repository": doc.get("github_repository"),
        "github_run_id": doc.get("github_run_id"),
        "trigger_detail": doc.get("trigger_detail"),
        "ts_iso": doc.get("ts_iso"),
        "policy": {
            "source": gov.get("policy_source"),
            "resolution": gov.get("policy_resolution"),
            "drift_any_environment": drift_any,
            "drift_by_environment": drift,
        },
        "status_by_environment": status_by_env,
        "reconciliation_coverage": recon_stats,
        "summary_line": doc.get("summary"),
    }


def render_markdown(rep: dict[str, Any]) -> str:
    pol = rep.get("policy") or {}
    lines = [
        "## On-call tracker handoff governance summary",
        "",
        f"- **repository**: `{rep.get('github_repository')}`",
        f"- **run_id**: `{rep.get('github_run_id')}`",
        f"- **trigger**: `{rep.get('trigger_detail')}`",
        f"- **policy_source**: `{pol.get('source')}`",
        f"- **policy_drift_any**: `{pol.get('drift_any_environment')}`",
        "",
        "### Environment status",
        "",
        "| environment | workflow status | reconciliation applicable | id captured |",
        "|-------------|-----------------|---------------------------|-------------|",
    ]
    rc = rep.get("reconciliation_coverage") or {}
    for env in ("staging", "production"):
        row = rc.get(env) or {}
        st = (rep.get("status_by_environment") or {}).get(env, "—")
        lines.append(
            f"| `{env}` | `{st}` | `{row.get('applicable')}` | `{row.get('has_key_or_url_hint')}` |"
        )
    lines.extend(["", f"_Handoff summary_: `{rep.get('summary_line')}`", ""])
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--handoff-json",
        type=Path,
        required=True,
        help="Path to oncall-tracker-handoff-results.json",
    )
    p.add_argument("--json-out", type=Path, required=True)
    p.add_argument("--markdown-out", type=Path, default=None)
    args = p.parse_args()

    hp = args.handoff_json.resolve()
    if not hp.is_file():
        print(json.dumps({"ok": False, "error": f"missing handoff results: {hp}"}))
        return 1
    try:
        doc = _load_handoff(hp)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    rep = summarize(doc)
    rep["ok"] = True
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(render_markdown(rep), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
