#!/usr/bin/env python3
"""Verify on-call route policy JSON: environment -> provider -> receiver/escalation mapping.

Local file and optional process-environment checks only (no HTTP, no provider APIs).

Optional root metadata (documented in deploy/ops example and ops docs):

- escalation_ownership_reviewed_at — ISO date (YYYY-MM-DD) or datetime of last escalation
  ownership review.
- escalation_ownership_max_age_days — positive int overriding the CLI default max age for
  staleness checks.

- oncall_route_policy_reviewed_at — ISO date or datetime of last on-call route policy review.
- oncall_route_policy_max_age_days — positive int overriding CLI default for policy freshness.

- verification_evidence_reviewed_at — ISO date or datetime of last verification / drill evidence
  review (e.g. scheduled verifier + synthetic drill).
- verification_evidence_max_age_days — positive int overriding CLI default for evidence age.

Per-environment (PagerDuty / Opsgenie):

- escalation_rotation_ref — non-empty string naming the primary on-call schedule or rotation
  (required when --require-escalation-rotation-ref is set, e.g. scheduled CI).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSIONS = frozenset({"1"})
DEFAULT_MAX_ESCALATION_OWNERSHIP_AGE_DAYS = 90
DEFAULT_MAX_ONCALL_ROUTE_POLICY_AGE_DAYS = 90
DEFAULT_MAX_VERIFICATION_EVIDENCE_AGE_DAYS = 90
ROTATION_REQUIRED_PROVIDERS = frozenset({"pagerduty", "opsgenie"})
ONCALL_PROVIDERS = frozenset({"pagerduty", "opsgenie", "alertmanager", "slack", "custom"})
SECRET_PROVIDERS = frozenset({"aws", "gcp", "vault"})
ROUTER_MODES = frozenset({"none", "file", "webhook"})


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_policy_calendar_date(value: Any) -> date | None:
    """Parse YYYY-MM-DD or ISO8601 datetime string to UTC calendar date."""
    if not _is_non_empty_str(value):
        return None
    s = str(value).strip()
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return date.fromisoformat(s[:10])
    except ValueError:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    except ValueError:
        return None


def validate_policy_metadata(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate optional root-level ownership / staleness metadata shape."""
    errors: list[str] = []
    warnings: list[str] = []

    raw_reviewed = doc.get("escalation_ownership_reviewed_at")
    if raw_reviewed is not None and raw_reviewed != "":
        if not _is_non_empty_str(raw_reviewed):
            errors.append("escalation_ownership_reviewed_at: must be a non-empty string if set")
        elif parse_policy_calendar_date(raw_reviewed) is None:
            errors.append(
                "escalation_ownership_reviewed_at: must be ISO date (YYYY-MM-DD) or ISO8601 datetime"
            )

    max_age = doc.get("escalation_ownership_max_age_days")
    if max_age is not None:
        if not isinstance(max_age, int) or isinstance(max_age, bool):
            errors.append("escalation_ownership_max_age_days: must be a positive integer if set")
        elif max_age <= 0:
            errors.append("escalation_ownership_max_age_days: must be a positive integer if set")

    raw_policy_rev = doc.get("oncall_route_policy_reviewed_at")
    if raw_policy_rev is not None and raw_policy_rev != "":
        if not _is_non_empty_str(raw_policy_rev):
            errors.append("oncall_route_policy_reviewed_at: must be a non-empty string if set")
        elif parse_policy_calendar_date(raw_policy_rev) is None:
            errors.append(
                "oncall_route_policy_reviewed_at: must be ISO date (YYYY-MM-DD) or ISO8601 datetime"
            )

    pol_max = doc.get("oncall_route_policy_max_age_days")
    if pol_max is not None:
        if not isinstance(pol_max, int) or isinstance(pol_max, bool):
            errors.append("oncall_route_policy_max_age_days: must be a positive integer if set")
        elif pol_max <= 0:
            errors.append("oncall_route_policy_max_age_days: must be a positive integer if set")

    raw_ev = doc.get("verification_evidence_reviewed_at")
    if raw_ev is not None and raw_ev != "":
        if not _is_non_empty_str(raw_ev):
            errors.append("verification_evidence_reviewed_at: must be a non-empty string if set")
        elif parse_policy_calendar_date(raw_ev) is None:
            errors.append(
                "verification_evidence_reviewed_at: must be ISO date (YYYY-MM-DD) or ISO8601 datetime"
            )

    ev_max = doc.get("verification_evidence_max_age_days")
    if ev_max is not None:
        if not isinstance(ev_max, int) or isinstance(ev_max, bool):
            errors.append("verification_evidence_max_age_days: must be a positive integer if set")
        elif ev_max <= 0:
            errors.append("verification_evidence_max_age_days: must be a positive integer if set")

    return errors, warnings


def verify_reviewed_at_staleness(
    doc: dict[str, Any],
    *,
    reviewed_field: str,
    max_age_field: str,
    reference_utc: datetime,
    cli_max_age_days: int,
    fail_on_stale: bool,
    missing_message: str,
    invalid_message: str,
    stale_message_fmt: str,
    future_warning_fmt: str,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Check a root *reviewed_at field against max age (policy *max_age_days or CLI default)."""
    errors: list[str] = []
    warnings: list[str] = []

    meta_max = doc.get(max_age_field)
    if isinstance(meta_max, int) and not isinstance(meta_max, bool) and meta_max > 0:
        max_age = meta_max
    else:
        max_age = max(1, int(cli_max_age_days))

    detail: dict[str, Any] = {
        "reviewed_field": reviewed_field,
        "max_age_field": max_age_field,
        "enabled": True,
        "fail_on_stale": fail_on_stale,
        "max_age_days": max_age,
        "reviewed_at": None,
        "age_days": None,
        "stale": False,
        "outcome": "ok",
    }

    raw = doc.get(reviewed_field)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        detail["stale"] = True
        detail["outcome"] = "missing_review_date"
        if fail_on_stale:
            errors.append(missing_message)
        else:
            warnings.append(missing_message)
        return errors, warnings, detail

    reviewed = parse_policy_calendar_date(raw)
    if reviewed is None:
        detail["stale"] = True
        detail["outcome"] = "invalid_review_date"
        errors.append(invalid_message)
        return errors, warnings, detail

    detail["reviewed_at"] = reviewed.isoformat()
    ref_day = reference_utc.astimezone(timezone.utc).date()
    age_days = (ref_day - reviewed).days
    detail["age_days"] = age_days

    if age_days < 0:
        warnings.append(future_warning_fmt.format(age_days=age_days))

    if age_days > max_age:
        detail["stale"] = True
        detail["outcome"] = "stale"
        msg = stale_message_fmt.format(age_days=age_days, max_age=max_age)
        if fail_on_stale:
            errors.append(msg)
        else:
            warnings.append(msg)

    return errors, warnings, detail


def verify_escalation_ownership_staleness(
    doc: dict[str, Any],
    *,
    reference_utc: datetime,
    cli_max_age_days: int,
    fail_on_stale: bool,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Check escalation_ownership_reviewed_at against max age (policy override or CLI default)."""
    return verify_reviewed_at_staleness(
        doc,
        reviewed_field="escalation_ownership_reviewed_at",
        max_age_field="escalation_ownership_max_age_days",
        reference_utc=reference_utc,
        cli_max_age_days=cli_max_age_days,
        fail_on_stale=fail_on_stale,
        missing_message=(
            "escalation_ownership_reviewed_at: missing; set when escalation ownership "
            "was last reviewed (ISO date)"
        ),
        invalid_message="escalation_ownership_reviewed_at: invalid ISO date",
        stale_message_fmt=(
            "escalation_ownership_reviewed_at is stale: age_days={age_days} "
            "> max_age_days={max_age}"
        ),
        future_warning_fmt=(
            "escalation_ownership_reviewed_at is in the future relative to reference time "
            "({age_days} days)"
        ),
    )


def verify_oncall_route_policy_freshness(
    doc: dict[str, Any],
    *,
    reference_utc: datetime,
    cli_max_age_days: int,
    fail_on_stale: bool,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Check oncall_route_policy_reviewed_at (policy mapping freshness)."""
    return verify_reviewed_at_staleness(
        doc,
        reviewed_field="oncall_route_policy_reviewed_at",
        max_age_field="oncall_route_policy_max_age_days",
        reference_utc=reference_utc,
        cli_max_age_days=cli_max_age_days,
        fail_on_stale=fail_on_stale,
        missing_message=(
            "oncall_route_policy_reviewed_at: missing; set when the on-call route policy "
            "was last reviewed (ISO date)"
        ),
        invalid_message="oncall_route_policy_reviewed_at: invalid ISO date",
        stale_message_fmt=(
            "oncall_route_policy_reviewed_at is stale: age_days={age_days} "
            "> max_age_days={max_age}"
        ),
        future_warning_fmt=(
            "oncall_route_policy_reviewed_at is in the future relative to reference time "
            "({age_days} days)"
        ),
    )


def verify_verification_evidence_age(
    doc: dict[str, Any],
    *,
    reference_utc: datetime,
    cli_max_age_days: int,
    fail_on_stale: bool,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Check verification_evidence_reviewed_at (drill / evidence review freshness)."""
    return verify_reviewed_at_staleness(
        doc,
        reviewed_field="verification_evidence_reviewed_at",
        max_age_field="verification_evidence_max_age_days",
        reference_utc=reference_utc,
        cli_max_age_days=cli_max_age_days,
        fail_on_stale=fail_on_stale,
        missing_message=(
            "verification_evidence_reviewed_at: missing; set when verification evidence "
            "was last reviewed (ISO date)"
        ),
        invalid_message="verification_evidence_reviewed_at: invalid ISO date",
        stale_message_fmt=(
            "verification_evidence_reviewed_at is stale: age_days={age_days} "
            "> max_age_days={max_age}"
        ),
        future_warning_fmt=(
            "verification_evidence_reviewed_at is in the future relative to reference time "
            "({age_days} days)"
        ),
    )


def verify_escalation_rotation_refs(
    doc: dict[str, Any], require: bool
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Ensure pagerduty/opsgenie rows document an escalation rotation reference when required."""
    errors: list[str] = []
    warnings: list[str] = []
    per_env: list[dict[str, Any]] = []

    envs = doc.get("environments")
    if not isinstance(envs, list):
        return errors, warnings, per_env

    for i, entry in enumerate(envs):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip() or f"environments[{i}]"
        oc = (entry.get("oncall_provider") or "")
        oc_key = oc.strip().lower() if _is_non_empty_str(str(oc)) else ""
        rotation_ref = entry.get("escalation_rotation_ref")
        has_ref = _is_non_empty_str(rotation_ref)

        row: dict[str, Any] = {
            "environment": name,
            "oncall_provider": oc_key or None,
            "rotation_ref_present": has_ref,
            "outcome": "skipped",
        }

        if oc_key not in ROTATION_REQUIRED_PROVIDERS:
            row["outcome"] = "not_applicable"
            per_env.append(row)
            continue

        if has_ref:
            row["outcome"] = "ok"
        elif require:
            row["outcome"] = "missing_rotation_ref"
            errors.append(
                f"{name}: escalation_rotation_ref required non-empty string for "
                f"oncall_provider={oc_key!r} (document PD schedule / Opsgenie rotation)"
            )
        else:
            row["outcome"] = "missing_rotation_ref_warn"
            warnings.append(
                f"{name}: escalation_rotation_ref empty for oncall_provider={oc_key!r}; "
                "document primary rotation or schedule ID for operations"
            )

        per_env.append(row)

    return errors, warnings, per_env


def validate_environment_entry(entry: dict[str, Any], index: int) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a single environment object."""
    errors: list[str] = []
    warnings: list[str] = []
    prefix = f"environments[{index}]"

    if not isinstance(entry, dict):
        return [f"{prefix}: must be an object"], []

    name = entry.get("name")
    if not _is_non_empty_str(name):
        errors.append(f"{prefix}.name: required non-empty string")

    sm = entry.get("secret_manager_provider")
    if not _is_non_empty_str(sm):
        errors.append(f"{prefix}.secret_manager_provider: required non-empty string")
    elif sm.strip().lower() not in SECRET_PROVIDERS:
        errors.append(
            f"{prefix}.secret_manager_provider: must be one of {sorted(SECRET_PROVIDERS)}"
        )

    oc = entry.get("oncall_provider")
    if not _is_non_empty_str(oc):
        errors.append(f"{prefix}.oncall_provider: required non-empty string")
    else:
        oc_l = oc.strip().lower()
        if oc_l not in ONCALL_PROVIDERS:
            errors.append(f"{prefix}.oncall_provider: must be one of {sorted(ONCALL_PROVIDERS)}")

    recv = entry.get("receiver_ref")
    if not _is_non_empty_str(recv):
        errors.append(f"{prefix}.receiver_ref: required non-empty string (logical receiver name)")

    esc = entry.get("escalation_policy_ref", "")
    esc_str = esc if isinstance(esc, str) else ""
    oc_key = (oc or "").strip().lower() if _is_non_empty_str(str(oc)) else ""
    if oc_key in ("pagerduty", "opsgenie") and not esc_str.strip():
        errors.append(
            f"{prefix}.escalation_policy_ref: required non-empty for oncall_provider={oc_key!r}"
        )
    if oc_key == "alertmanager" and esc_str.strip():
        warnings.append(
            f"{prefix}: escalation_policy_ref set for alertmanager; "
            "usually unused (use Alertmanager route + receiver instead)."
        )
    if oc_key == "custom" and not esc_str.strip():
        warnings.append(
            f"{prefix}: custom oncall_provider without escalation_policy_ref; "
            "document escalation in notes or CMDB."
        )

    mode = entry.get("in_process_ops_alert_router_mode")
    if mode is not None:
        if not _is_non_empty_str(mode):
            errors.append(f"{prefix}.in_process_ops_alert_router_mode: must be non-empty if set")
        elif str(mode).strip().lower() not in ROUTER_MODES:
            errors.append(
                f"{prefix}.in_process_ops_alert_router_mode: must be one of {sorted(ROUTER_MODES)}"
            )

    labels = entry.get("prometheus_alert_route_labels")
    if oc_key == "alertmanager":
        if not isinstance(labels, dict) or not labels:
            errors.append(
                f"{prefix}.prometheus_alert_route_labels: required non-empty object for "
                "alertmanager (match keys used in Alertmanager routes)"
            )
        else:
            for k, v in labels.items():
                if not isinstance(k, str) or not k.strip():
                    errors.append(f"{prefix}.prometheus_alert_route_labels: invalid key {k!r}")
                if not isinstance(v, str) or not v.strip():
                    errors.append(
                        f"{prefix}.prometheus_alert_route_labels[{k!r}]: must be non-empty string"
                    )
    elif labels is not None and not isinstance(labels, dict):
        errors.append(f"{prefix}.prometheus_alert_route_labels: must be an object if set")

    return errors, warnings


def verify_policy_document(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate full policy document. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(doc, dict):
        return ["root must be a JSON object"], []

    m_err, m_warn = validate_policy_metadata(doc)
    errors.extend(m_err)
    warnings.extend(m_warn)

    ver = doc.get("schema_version")
    if ver is None:
        errors.append("schema_version: required")
    elif str(ver) not in SCHEMA_VERSIONS:
        errors.append(f"schema_version: unsupported {ver!r}; supported {sorted(SCHEMA_VERSIONS)}")

    envs = doc.get("environments")
    if envs is None:
        errors.append("environments: required array")
    elif not isinstance(envs, list):
        errors.append("environments: must be an array")
    elif len(envs) == 0:
        warnings.append("environments: empty array (no environments registered)")
    else:
        names: list[str] = []
        for i, entry in enumerate(envs):
            e_err, e_warn = validate_environment_entry(entry, i)
            errors.extend(e_err)
            warnings.extend(e_warn)
            if isinstance(entry, dict) and _is_non_empty_str(entry.get("name")):
                names.append(str(entry["name"]).strip())
        if len(names) != len(set(names)):
            errors.append("environments: duplicate name entries are not allowed")

    return errors, warnings


def load_policy(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("policy root must be a JSON object")
    return data


def find_environment(doc: dict[str, Any], name: str) -> dict[str, Any] | None:
    envs = doc.get("environments")
    if not isinstance(envs, list):
        return None
    want = name.strip()
    for entry in envs:
        if isinstance(entry, dict) and str(entry.get("name", "")).strip() == want:
            return entry
    return None


def verify_process_env_against_entry(entry: dict[str, Any]) -> list[str]:
    """Compare os.environ to policy entry; return error strings."""
    errors: list[str] = []
    sm_env = (os.environ.get("COHERENCE_FUND_SECRET_MANAGER_PROVIDER") or "").strip().lower()
    sm_pol = str(entry.get("secret_manager_provider") or "").strip().lower()
    if sm_env and sm_pol and sm_env != sm_pol:
        errors.append(
            f"env COHERENCE_FUND_SECRET_MANAGER_PROVIDER={sm_env!r} != policy "
            f"secret_manager_provider={sm_pol!r}"
        )

    mode_env = (os.environ.get("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE") or "").strip().lower()
    if not mode_env:
        mode_env = "none"
    mode_pol = entry.get("in_process_ops_alert_router_mode")
    if mode_pol is not None:
        mp = str(mode_pol).strip().lower()
        if mode_env != mp:
            errors.append(
                f"env COHERENCE_FUND_OPS_ALERT_ROUTER_MODE={mode_env!r} != policy "
                f"in_process_ops_alert_router_mode={mp!r}"
            )
    return errors


def build_result(
    *,
    policy_path: str,
    errors: list[str],
    warnings: list[str],
    env_check_name: str | None,
    env_check_errors: list[str],
    staleness: dict[str, Any] | None,
    policy_freshness: dict[str, Any] | None,
    verification_evidence: dict[str, Any] | None,
    rotation_check: dict[str, Any] | None,
) -> dict[str, Any]:
    all_errors = list(errors) + list(env_check_errors)
    out: dict[str, Any] = {
        "ok": len(all_errors) == 0,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "policy_path": policy_path,
        "error_count": len(all_errors),
        "warning_count": len(warnings),
        "errors": all_errors,
        "warnings": warnings,
        "env_check_environment": env_check_name,
        "env_check_errors": env_check_errors,
    }
    if staleness is not None:
        out["staleness"] = staleness
    if policy_freshness is not None:
        out["policy_freshness"] = policy_freshness
    if verification_evidence is not None:
        out["verification_evidence"] = verification_evidence
    if rotation_check is not None:
        out["rotation_check"] = rotation_check
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_policy = Path(__file__).resolve().parents[1] / "ops" / "oncall-route-policy.example.json"
    parser.add_argument(
        "--policy",
        type=Path,
        default=default_policy,
        help=f"Path to on-call route policy JSON (default: {default_policy})",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write verification result JSON to this path",
    )
    parser.add_argument(
        "--env",
        type=str,
        default=None,
        help="Environment name to match for --check-env",
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Compare COHERENCE_FUND_SECRET_MANAGER_PROVIDER and "
        "COHERENCE_FUND_OPS_ALERT_ROUTER_MODE to the selected --env policy row",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures (exit 1)",
    )
    parser.add_argument(
        "--fail-on-stale-escalation-ownership",
        action="store_true",
        help="Fail if escalation_ownership_reviewed_at is missing, invalid, or older than max age",
    )
    parser.add_argument(
        "--max-escalation-ownership-age-days",
        type=int,
        default=DEFAULT_MAX_ESCALATION_OWNERSHIP_AGE_DAYS,
        metavar="N",
        help=(
            "Maximum allowed age in days for escalation_ownership_reviewed_at "
            f"(default: {DEFAULT_MAX_ESCALATION_OWNERSHIP_AGE_DAYS}; overridden by "
            "policy escalation_ownership_max_age_days when set)"
        ),
    )
    parser.add_argument(
        "--fail-on-stale-oncall-route-policy",
        action="store_true",
        help=(
            "Fail if oncall_route_policy_reviewed_at is missing, invalid, or older than max age"
        ),
    )
    parser.add_argument(
        "--max-oncall-route-policy-age-days",
        type=int,
        default=DEFAULT_MAX_ONCALL_ROUTE_POLICY_AGE_DAYS,
        metavar="N",
        help=(
            "Maximum allowed age in days for oncall_route_policy_reviewed_at "
            f"(default: {DEFAULT_MAX_ONCALL_ROUTE_POLICY_AGE_DAYS}; overridden by "
            "policy oncall_route_policy_max_age_days when set)"
        ),
    )
    parser.add_argument(
        "--fail-on-stale-verification-evidence",
        action="store_true",
        help=(
            "Fail if verification_evidence_reviewed_at is missing, invalid, or older than max age"
        ),
    )
    parser.add_argument(
        "--max-verification-evidence-age-days",
        type=int,
        default=DEFAULT_MAX_VERIFICATION_EVIDENCE_AGE_DAYS,
        metavar="N",
        help=(
            "Maximum allowed age in days for verification_evidence_reviewed_at "
            f"(default: {DEFAULT_MAX_VERIFICATION_EVIDENCE_AGE_DAYS}; overridden by "
            "policy verification_evidence_max_age_days when set)"
        ),
    )
    parser.add_argument(
        "--require-escalation-rotation-ref",
        action="store_true",
        help=(
            "Require non-empty escalation_rotation_ref for pagerduty and opsgenie "
            "environment rows"
        ),
    )
    parser.add_argument(
        "--reference-time",
        type=str,
        default=None,
        metavar="ISO8601",
        help=(
            "UTC reference instant for staleness age (testing only); "
            "default is current UTC time"
        ),
    )
    args = parser.parse_args()

    policy_path = args.policy.resolve()
    env_check_errors: list[str] = []
    env_name: str | None = None

    try:
        doc = load_policy(policy_path)
    except FileNotFoundError:
        result = build_result(
            policy_path=str(policy_path),
            errors=[f"policy file not found: {policy_path}"],
            warnings=[],
            env_check_name=None,
            env_check_errors=[],
            staleness=None,
            policy_freshness=None,
            verification_evidence=None,
            rotation_check=None,
        )
        print(json.dumps(result, indent=2))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        result = build_result(
            policy_path=str(policy_path),
            errors=[f"invalid policy JSON: {e}"],
            warnings=[],
            env_check_name=None,
            env_check_errors=[],
            staleness=None,
            policy_freshness=None,
            verification_evidence=None,
            rotation_check=None,
        )
        print(json.dumps(result, indent=2))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    errors, warnings = verify_policy_document(doc)

    ref_time = datetime.now(timezone.utc)
    if args.reference_time:
        try:
            rs = args.reference_time.strip().replace("Z", "+00:00")
            ref_time = datetime.fromisoformat(rs)
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            else:
                ref_time = ref_time.astimezone(timezone.utc)
        except ValueError:
            print(
                f"invalid --reference-time {args.reference_time!r} (use ISO8601)",
                file=sys.stderr,
            )
            return 2

    st_err, st_warn, staleness_detail = verify_escalation_ownership_staleness(
        doc,
        reference_utc=ref_time,
        cli_max_age_days=max(1, int(args.max_escalation_ownership_age_days)),
        fail_on_stale=bool(args.fail_on_stale_escalation_ownership),
    )
    errors.extend(st_err)
    warnings.extend(st_warn)

    pf_err, pf_warn, policy_freshness_detail = verify_oncall_route_policy_freshness(
        doc,
        reference_utc=ref_time,
        cli_max_age_days=max(1, int(args.max_oncall_route_policy_age_days)),
        fail_on_stale=bool(args.fail_on_stale_oncall_route_policy),
    )
    errors.extend(pf_err)
    warnings.extend(pf_warn)

    ve_err, ve_warn, verification_evidence_detail = verify_verification_evidence_age(
        doc,
        reference_utc=ref_time,
        cli_max_age_days=max(1, int(args.max_verification_evidence_age_days)),
        fail_on_stale=bool(args.fail_on_stale_verification_evidence),
    )
    errors.extend(ve_err)
    warnings.extend(ve_warn)

    rot_err, rot_warn, rotation_rows = verify_escalation_rotation_refs(
        doc, require=bool(args.require_escalation_rotation_ref)
    )
    errors.extend(rot_err)
    warnings.extend(rot_warn)
    rotation_check_detail: dict[str, Any] = {
        "require_escalation_rotation_ref": bool(args.require_escalation_rotation_ref),
        "environments": rotation_rows,
        "outcome": "ok"
        if not rot_err
        else "failed",
    }

    if args.check_env:
        if not args.env or not str(args.env).strip():
            print("--check-env requires --env NAME", file=sys.stderr)
            return 2
        env_name = str(args.env).strip()
        entry = find_environment(doc, env_name)
        if entry is None:
            env_check_errors.append(f"no policy environment named {env_name!r}")
        else:
            env_check_errors.extend(verify_process_env_against_entry(entry))

    result = build_result(
        policy_path=str(policy_path),
        errors=errors,
        warnings=warnings,
        env_check_name=env_name,
        env_check_errors=env_check_errors,
        staleness={
            **staleness_detail,
            "reference_time_utc": ref_time.isoformat().replace("+00:00", "Z"),
        },
        policy_freshness={
            **policy_freshness_detail,
            "reference_time_utc": ref_time.isoformat().replace("+00:00", "Z"),
        },
        verification_evidence={
            **verification_evidence_detail,
            "reference_time_utc": ref_time.isoformat().replace("+00:00", "Z"),
        },
        rotation_check=rotation_check_detail,
    )

    print(json.dumps(result, indent=2))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    fail = not result["ok"] or (args.strict and warnings)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
