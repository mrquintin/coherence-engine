#!/usr/bin/env python3
"""Governance attestation aging reports and ownership-attestation validation for CI.

- `report`: reads a governance baselines JSON (same schema as verify_uncertainty_policy_baselines)
  and emits per-environment ages for change_review.last_baseline_approved_at vs policy max-age.
  No network; optional --as-of-date for deterministic runs.

- `validate-ownership-attestation`: enforces promotion-time ownership attestation freshness
  from environment variables (GitHub Actions). On failure, writes a machine-readable escalation
  record to ESCALATION_OUT (default artifacts/governance_attestation_escalation.json).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Reuse baseline parsing/validation from the verifier (local files only).
from verify_uncertainty_policy_baselines import (
    load_json_object,
    parse_iso_date,
    validate_baseline_root,
)


def _utc_today(as_of: date | None) -> date:
    if as_of is not None:
        return as_of
    return datetime.now(timezone.utc).date()


def _parse_int(label: str, raw: str | None, default: int | None) -> int:
    s = (raw or "").strip()
    if not s:
        if default is None:
            raise ValueError(f"internal: no default for {label}")
        return default
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"{label} must be a non-negative integer") from None
    if v < 0:
        raise ValueError(f"{label} must be non-negative")
    return v


def _escalation_paths_common() -> list[dict[str, Any]]:
    return [
        {
            "id": "block_promotion",
            "action": "workflow_failure",
            "description": "Promotion blocked until attestation is renewed within policy max age.",
        },
        {
            "id": "notify_policy_owner",
            "action": "manual_contact",
            "reference": "vars.UNCERTAINTY_POLICY_OWNING_TEAM",
            "description": "Notify owning team; renew ownership attestation after governance review.",
        },
        {
            "id": "renew_workflow_inputs",
            "action": "workflow_dispatch_inputs",
            "reference": "policy_ownership_attestation_effective_date",
            "description": "Re-run with a new ISO YYYY-MM-DD attestation effective date after approval.",
        },
        {
            "id": "scheduled_aging_report",
            "action": "github_workflow",
            "reference": ".github/workflows/uncertainty-attestation-lifecycle.yml",
            "description": "Review scheduled governance-attestation-aging-report artifacts for lead time.",
        },
        {
            "id": "escalation_artifact",
            "action": "github_actions_artifact",
            "artifact_name": "governance-attestation-escalation",
            "file": "governance_attestation_escalation.json",
        },
    ]


def write_escalation_record(
    out_path: Path,
    *,
    breach_kind: str,
    eff_raw: str | None,
    age_days: int | None,
    max_age_days: int,
    reminder_days_before_max: int,
    reminder_window_start_age_days: int,
    detail_message: str,
    workflow_file: str,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    promo = (os.environ.get("GOVERNANCE_PROMOTION_ENV") or "").strip()
    gh_env_ctx = (os.environ.get("GOVERNANCE_GITHUB_ENVIRONMENT") or "").strip()
    if not gh_env_ctx:
        gh_env_ctx = (os.environ.get("GITHUB_ENVIRONMENT") or "").strip()
    promotion_context: dict[str, Any] = {
        "promotion_environment": promo or None,
        "github_environment": gh_env_ctx or None,
    }

    rec = {
        "schema_version": 1,
        "record_type": "governance_attestation_escalation",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "workflow_file": workflow_file,
            "github_event_name": os.environ.get("GITHUB_EVENT_NAME"),
            "github_repository": os.environ.get("GITHUB_REPOSITORY"),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        },
        "promotion_context": promotion_context,
        "breach": {
            "kind": breach_kind,
            "policy_ownership_attestation_effective_date": eff_raw,
            "age_days": age_days,
            "max_age_days_policy": max_age_days,
            "reminder_days_before_max": reminder_days_before_max,
            "reminder_window_start_age_days": reminder_window_start_age_days,
            "detail": detail_message,
        },
        "escalation_paths": _escalation_paths_common(),
    }
    out_path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_validate_ownership_attestation(_args: argparse.Namespace) -> int:
    out = Path(os.environ.get("ESCALATION_OUT") or "artifacts/governance_attestation_escalation.json")
    wf_file = os.environ.get("GOVERNANCE_ATTESTATION_WORKFLOW_FILE") or "workflow"

    def fail(
        kind: str,
        msg: str,
        *,
        eff_raw: str | None = None,
        age_days: int | None = None,
        max_age: int = 90,
        rem: int = 14,
    ) -> int:
        warn_from = max(0, max_age - rem)
        write_escalation_record(
            out,
            breach_kind=kind,
            eff_raw=eff_raw,
            age_days=age_days,
            max_age_days=max_age,
            reminder_days_before_max=rem,
            reminder_window_start_age_days=warn_from,
            detail_message=msg,
            workflow_file=wf_file,
        )
        print(
            f"::error title=Stale or invalid ownership attestation::"
            f"{msg} Machine-readable escalation: {out} (upload artifact governance-attestation-escalation)."
        )
        return 1

    try:
        max_age_def = _parse_int(
            "UNCERTAINTY_OWNERSHIP_ATTESTATION_MAX_AGE_DAYS",
            os.environ.get("MAX_VAR"),
            90,
        )
        max_age = _parse_int("ownership_attestation_max_age_days", os.environ.get("MAX_IN"), max_age_def)
        rem_def = _parse_int(
            "UNCERTAINTY_OWNERSHIP_ATTESTATION_REMINDER_DAYS_BEFORE_MAX",
            os.environ.get("REM_VAR"),
            14,
        )
        rem = _parse_int(
            "ownership_attestation_reminder_days_before_max",
            os.environ.get("REM_IN"),
            rem_def,
        )
    except ValueError as e:
        return fail("ownership_attestation_invalid", str(e), max_age=90, rem=14)

    eff_raw = (os.environ.get("EFF") or "").strip()
    if not eff_raw:
        return fail(
            "ownership_attestation_invalid",
            "policy_ownership_attestation_effective_date is required (YYYY-MM-DD) for promotion environments.",
            eff_raw=eff_raw or None,
            max_age=max_age,
            rem=rem,
        )

    try:
        eff = date.fromisoformat(eff_raw[:10])
    except ValueError:
        return fail(
            "ownership_attestation_invalid",
            "policy_ownership_attestation_effective_date must be YYYY-MM-DD",
            eff_raw=eff_raw,
            max_age=max_age,
            rem=rem,
        )

    today = datetime.now(timezone.utc).date()
    age = (today - eff).days
    warn_from = max(0, max_age - rem)

    if age < 0:
        return fail(
            "ownership_attestation_future_effective_date",
            "policy_ownership_attestation_effective_date is in the future (UTC calendar date).",
            eff_raw=eff_raw,
            age_days=age,
            max_age=max_age,
            rem=rem,
        )

    if age > max_age:
        return fail(
            "ownership_attestation_stale",
            f"Attestation effective date {eff_raw} is {age} days old (UTC); max allowed is {max_age} days. "
            "Renew the attestation after governance review before promoting.",
            eff_raw=eff_raw,
            age_days=age,
            max_age=max_age,
            rem=rem,
        )

    if age >= warn_from:
        msg = (
            f"Attestation age {age} days — within reminder window (stale after {max_age} days; "
            f"reminders from {warn_from} days). Plan rotation before promotions are blocked."
        )
        safe = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning title=Ownership attestation rotation reminder::{safe}")

    print(f"Ownership attestation age OK (age_days={age}, max_age_days={max_age}, reminder_from_days={warn_from}).")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    baselines_path: Path = args.baselines.resolve()
    as_of = None
    if args.as_of_date:
        as_of = date.fromisoformat(args.as_of_date.strip()[:10])

    try:
        doc = load_json_object(baselines_path)
    except FileNotFoundError:
        print(json.dumps({"ok": False, "error": f"baselines file not found: {baselines_path}"}))
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"invalid baselines: {e}"}))
        return 1

    val_err = validate_baseline_root(doc)
    if val_err:
        print(
            json.dumps(
                {
                    "ok": False,
                    "validation_errors": val_err,
                    "reported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                indent=2,
            )
        )
        return 1

    max_age = args.max_age_days
    rem = args.reminder_days_before_max
    warn_from = max(0, max_age - rem)
    today = _utc_today(as_of)

    envs = doc.get("environments")
    rows: list[dict[str, Any]] = []
    if isinstance(envs, dict):
        for name in sorted(envs.keys()):
            entry = envs[name]
            approved_raw: str | None = None
            parsed: date | None = None
            if isinstance(entry, dict):
                cr = entry.get("change_review")
                if isinstance(cr, dict):
                    approved_raw = cr.get("last_baseline_approved_at")
                    if isinstance(approved_raw, str):
                        parsed = parse_iso_date(approved_raw)

            age_days: int | None = None
            status = "unknown"
            note: str | None = None
            if parsed is not None:
                age_days = (today - parsed).days
                if age_days < 0:
                    status = "invalid_future_approval_date"
                    note = "last_baseline_approved_at is in the future relative to as_of date"
                elif age_days > max_age:
                    status = "stale"
                elif age_days >= warn_from:
                    status = "reminder"
                else:
                    status = "ok"
            else:
                status = "missing_or_unparseable_date"
                note = "could not parse change_review.last_baseline_approved_at"

            rows.append(
                {
                    "environment": str(name).strip(),
                    "last_baseline_approved_at": approved_raw,
                    "parsed_approval_date": parsed.isoformat() if parsed else None,
                    "age_days": age_days,
                    "max_age_days_policy": max_age,
                    "reminder_days_before_max": rem,
                    "reminder_window_start_age_days": warn_from,
                    "status": status,
                    "note": note,
                }
            )

    source_repo = (getattr(args, "source_repository", None) or "").strip() or None
    if source_repo is None:
        source_repo = (os.environ.get("GOVERNANCE_ATTESTATION_REPORT_SOURCE_REPO") or "").strip() or None
    if source_repo is None:
        gh_repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
        source_repo = gh_repo or None

    result: dict[str, Any] = {
        "ok": True,
        "report_kind": "governance_baseline_approval_age",
        "reported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of_utc_date": today.isoformat(),
        "baselines_path": str(baselines_path),
        "max_age_days": max_age,
        "reminder_days_before_max": rem,
        "environments": rows,
        "source_repository": source_repo,
        "source_workflow_run_id": (os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "reminder_artifact_hint": (
            "Environments in reminder or stale status should renew baseline approval metadata "
            "and update UNCERTAINTY_GOVERNANCE_POLICY_BASELINES_JSON after change review."
        ),
    }

    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    else:
        print(text)

    if args.markdown_out:
        md = _render_markdown_summary(result)
        mp = Path(args.markdown_out)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(md, encoding="utf-8")

    gh = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh and args.write_step_summary:
        with open(gh, "a", encoding="utf-8") as fh:
            fh.write(_render_markdown_summary(result))

    return 0


def _render_markdown_summary(result: dict[str, Any]) -> str:
    lines = [
        "## Governance attestation aging (baseline approval dates)",
        "",
        f"- **as_of_utc_date**: `{result.get('as_of_utc_date')}`",
        f"- **baselines_path**: `{result.get('baselines_path')}`",
        f"- **max_age_days**: `{result.get('max_age_days')}`",
        f"- **reminder_days_before_max**: `{result.get('reminder_days_before_max')}`",
        "",
        "| environment | last_baseline_approved_at | age_days | status |",
        "|-------------|---------------------------|----------|--------|",
    ]
    for row in result.get("environments") or []:
        lines.append(
            "| {env} | `{appr}` | `{age}` | `{st}` |".format(
                env=row.get("environment", ""),
                appr=row.get("last_baseline_approved_at") or "",
                age=row.get("age_days") if row.get("age_days") is not None else "",
                st=row.get("status", ""),
            )
        )
    lines.extend(
        [
            "",
            str(result.get("reminder_artifact_hint") or ""),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_rep = sub.add_parser("report", help="Emit JSON (and optional Markdown) aging report from baselines")
    p_rep.add_argument(
        "--baselines",
        type=Path,
        required=True,
        help="Path to governance policy baselines JSON",
    )
    p_rep.add_argument(
        "--max-age-days",
        type=int,
        default=90,
        help="Max age in days for last_baseline_approved_at (default 90)",
    )
    p_rep.add_argument(
        "--reminder-days-before-max",
        type=int,
        default=14,
        help="Reminder window length before max age (default 14)",
    )
    p_rep.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        help="UTC calendar date YYYY-MM-DD for age calculation (default: today UTC)",
    )
    p_rep.add_argument("--json-out", type=Path, default=None, help="Write report JSON to this path")
    p_rep.add_argument("--markdown-out", type=Path, default=None, help="Write Markdown summary to this path")
    p_rep.add_argument(
        "--write-step-summary",
        action="store_true",
        help="Append Markdown summary to GITHUB_STEP_SUMMARY when set",
    )
    p_rep.add_argument(
        "--source-repository",
        type=str,
        default=None,
        help="Label this report for aggregation (overrides GITHUB_REPOSITORY / GOVERNANCE_ATTESTATION_REPORT_SOURCE_REPO)",
    )
    p_rep.set_defaults(func=cmd_report)

    p_val = sub.add_parser(
        "validate-ownership-attestation",
        help="Validate EFF/MAX_*/REM_* env vars; write escalation JSON on failure",
    )
    p_val.set_defaults(func=cmd_validate_ownership_attestation)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
