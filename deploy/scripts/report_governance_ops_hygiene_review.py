#!/usr/bin/env python3
"""Aggregate governance / on-call CI artifact JSON into a single review bundle (local files only).

Intended for periodic ops hygiene: download latest workflow artifacts (or point at saved
copies), then run this script with optional paths. Missing inputs are recorded as ``skipped``
with no failure (exit 0).

No network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REPORT_KIND = "governance_ops_hygiene_review"


def _read_json(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    if not path.is_file():
        return None, f"not found: {path}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, str(e)
    if not isinstance(doc, dict):
        return None, "root must be object"
    return doc, None


def _summarize_enrollment(doc: dict[str, Any]) -> dict[str, Any]:
    s = doc.get("summary") or {}
    return {
        "report_kind": doc.get("report_kind"),
        "compliant": doc.get("compliant"),
        "expected_count": s.get("expected_count"),
        "observed_count": s.get("observed_count"),
        "missing_count": s.get("missing_count"),
        "manifest_errors_count": len(doc.get("manifest_errors") or []),
    }


def _summarize_webhook_receipt(doc: dict[str, Any]) -> dict[str, Any]:
    ch = doc.get("channels") or []
    modes = []
    for row in ch:
        if not isinstance(row, dict):
            continue
        d = row.get("delivery") or {}
        if isinstance(d, dict):
            modes.append(d.get("mode"))
    return {
        "record_type": doc.get("record_type"),
        "hmac_key_configured": doc.get("hmac_key_configured"),
        "live_post_requested": doc.get("live_post_requested"),
        "force_dry_run": doc.get("force_dry_run"),
        "channel_count": len(ch) if isinstance(ch, list) else 0,
        "delivery_modes": modes,
    }


def _summarize_handoff_gov(doc: dict[str, Any]) -> dict[str, Any]:
    pol = doc.get("policy") or {}
    return {
        "report_kind": doc.get("report_kind"),
        "policy_source": pol.get("source"),
        "policy_drift_any": pol.get("drift_any_environment"),
        "status_by_environment": doc.get("status_by_environment"),
        "reconciliation_coverage": doc.get("reconciliation_coverage"),
    }


def _summarize_routing_proof(doc: dict[str, Any]) -> dict[str, Any]:
    ch = doc.get("channels") or []
    urls = []
    for row in ch:
        if not isinstance(row, dict):
            continue
        r = row.get("routing") or {}
        if isinstance(r, dict):
            urls.append(r.get("webhook_url_configured"))
    return {
        "record_type": doc.get("record_type"),
        "channel_count": len(ch) if isinstance(ch, list) else 0,
        "webhook_url_configured_flags": urls,
    }


def build_report(
    *,
    enrollment: dict[str, Any] | None,
    webhook_receipt: dict[str, Any] | None,
    handoff_governance: dict[str, Any] | None,
    routing_proof: dict[str, Any] | None,
    errors: dict[str, str | None],
) -> dict[str, Any]:
    sections: dict[str, Any] = {}
    reminders: list[str] = []

    if enrollment is not None:
        sections["enrollment_coverage"] = _summarize_enrollment(enrollment)
        if enrollment.get("compliant") is False:
            reminders.append(
                "Enrollment coverage is not compliant — review missing_repositories and manifest_errors in source JSON."
            )
    else:
        sections["enrollment_coverage"] = {"skipped": True, "reason": errors.get("enrollment")}

    if webhook_receipt is not None:
        sections["escalation_webhook_delivery_receipt"] = _summarize_webhook_receipt(webhook_receipt)
        if webhook_receipt.get("live_post_requested") and not webhook_receipt.get("force_dry_run"):
            reminders.append(
                "Webhook receipt run requested live POST — confirm canary/prod URLs and approvals match policy."
            )
    else:
        sections["escalation_webhook_delivery_receipt"] = {
            "skipped": True,
            "reason": errors.get("webhook_receipt"),
        }

    if handoff_governance is not None:
        sections["tracker_handoff_governance"] = _summarize_handoff_gov(handoff_governance)
        if handoff_governance.get("policy", {}).get("drift_any_environment"):
            reminders.append(
                "Tracker handoff policy drift vs built-ins detected — review governance JSON and overlay precedence."
            )
    else:
        sections["tracker_handoff_governance"] = {
            "skipped": True,
            "reason": errors.get("handoff_governance"),
        }

    if routing_proof is not None:
        sections["escalation_routing_proof"] = _summarize_routing_proof(routing_proof)
    else:
        sections["escalation_routing_proof"] = {"skipped": True, "reason": errors.get("routing_proof")}

    reminders.extend(
        [
            "Rotate or validate secrets when routing maps change: GOVERNANCE_ESCALATION_ROUTING_MAP_JSON, "
            "GOVERNANCE_ESCALATION_RECEIPT_HMAC_KEY, GOVERNANCE_ENROLLMENT_* , tracker handoff URLs.",
            "Re-download CI artifacts after each scheduled run for audit archives.",
        ]
    )

    return {
        "schema_version": 1,
        "report_kind": REPORT_KIND,
        "sections": sections,
        "reminders": reminders,
        "input_errors": {k: v for k, v in errors.items() if v},
    }


def render_markdown(rep: dict[str, Any]) -> str:
    lines = [
        "## Governance / on-call ops hygiene review",
        "",
        "Aggregated from local CI artifact JSON copies (no network).",
        "",
    ]
    for name, block in (rep.get("sections") or {}).items():
        lines.append(f"### `{name}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(block, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    lines.append("### Reminders")
    lines.append("")
    for r in rep.get("reminders") or []:
        lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--enrollment-json",
        type=Path,
        default=None,
        help="governance-enrollment-coverage.json (optional)",
    )
    p.add_argument(
        "--webhook-receipt-json",
        type=Path,
        default=None,
        help="governance_escalation_webhook_delivery_receipt artifact (optional)",
    )
    p.add_argument(
        "--handoff-governance-json",
        type=Path,
        default=None,
        help="oncall-tracker-handoff-governance.json (optional)",
    )
    p.add_argument(
        "--routing-proof-json",
        type=Path,
        default=None,
        help="governance_escalation_routing_proof.json (optional)",
    )
    p.add_argument("--json-out", type=Path, required=True)
    p.add_argument("--markdown-out", type=Path, default=None)
    args = p.parse_args()

    errors: dict[str, str | None] = {}
    enrollment, e1 = _read_json(args.enrollment_json)
    errors["enrollment"] = e1
    webhook, e2 = _read_json(args.webhook_receipt_json)
    errors["webhook_receipt"] = e2
    handoff, e3 = _read_json(args.handoff_governance_json)
    errors["handoff_governance"] = e3
    routing, e4 = _read_json(args.routing_proof_json)
    errors["routing_proof"] = e4

    rep = build_report(
        enrollment=enrollment,
        webhook_receipt=webhook,
        handoff_governance=handoff,
        routing_proof=routing,
        errors=errors,
    )
    rep["ok"] = True

    out = args.json_out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(render_markdown(rep), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
