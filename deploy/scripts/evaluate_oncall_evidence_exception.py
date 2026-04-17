#!/usr/bin/env python3
"""Evaluate live-drill evidence freshness gate with optional snooze/exception records.

Reads machine-readable state from ``live-drill-evidence-state.json`` (produced by the
workflow discovery step). With no exception JSON, stale evidence is denied. With a
valid, matching exception, stale evidence can be allowed until ``expires_at``.

Optional **policy-as-code** (``--policy``) enforces max snooze duration, allowed reason
codes, required snooze fields, gate-specific limits, and requires ``environment`` on
the exception record.

**Approval artifact** mode (``--approve-artifact-out``) validates a proposed exception
JSON against a policy file and writes a signed-off artifact for auditors — no outbound
network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALLOWED_TOP_KEYS = frozenset(
    {"schema_version", "gate", "snooze", "github_repository", "environment"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_utc(s: str) -> datetime:
    s = s.strip()
    if not s:
        raise ValueError("empty timestamp")
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _write_decision(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_policy_file(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not path.is_file():
        errors.append(f"Policy file not found: {path}")
        return None, errors
    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        errors.append(f"Policy file is not valid JSON: {e}")
        return None, errors
    if not isinstance(doc, dict):
        errors.append("Policy root must be a JSON object.")
        return None, errors
    ver = doc.get("schema_version")
    if ver != 1:
        errors.append(f"policy.schema_version must be 1, got {ver!r}.")
        return None, errors

    max_h = doc.get("max_snooze_duration_hours")
    if max_h is not None and (
        isinstance(max_h, bool) or not isinstance(max_h, (int, float))
    ):
        errors.append("policy.max_snooze_duration_hours must be a number when set.")
        return None, errors

    arc = doc.get("allowed_reason_codes")
    if arc is not None:
        if not isinstance(arc, list):
            errors.append("policy.allowed_reason_codes must be a list when set.")
            return None, errors
        for i, c in enumerate(arc):
            if not isinstance(c, str) or not c.strip():
                errors.append(
                    f"policy.allowed_reason_codes[{i}] must be a non-empty string."
                )
                return None, errors

    srf = doc.get("snooze_required_fields")
    if srf is not None:
        if not isinstance(srf, list):
            errors.append("policy.snooze_required_fields must be a list when set.")
            return None, errors
        for i, f in enumerate(srf):
            if not isinstance(f, str) or not f.strip():
                errors.append(
                    f"policy.snooze_required_fields[{i}] must be a non-empty string."
                )
                return None, errors

    extra = set(doc.keys()) - {
        "schema_version",
        "max_snooze_duration_hours",
        "allowed_reason_codes",
        "snooze_required_fields",
        "gates",
    }
    if extra:
        errors.append(f"Unknown top-level keys in policy: {sorted(extra)}")
        return None, errors
    gates = doc.get("gates")
    if gates is not None and not isinstance(gates, dict):
        errors.append("policy.gates must be a JSON object when set.")
        return None, errors
    if gates:
        for gk, gv in gates.items():
            if not isinstance(gk, str):
                errors.append("policy.gates keys must be strings.")
                return None, errors
            if not isinstance(gv, dict):
                errors.append(f"policy.gates[{gk!r}] must be a JSON object.")
                return None, errors
            bad = set(gv.keys()) - {
                "max_snooze_duration_hours",
                "allowed_reason_codes",
                "snooze_required_fields",
            }
            if bad:
                errors.append(f"Unknown keys in policy.gates[{gk!r}]: {sorted(bad)}")
                return None, errors
            gh = gv.get("max_snooze_duration_hours")
            if gh is not None and (
                isinstance(gh, bool) or not isinstance(gh, (int, float))
            ):
                errors.append(
                    f"policy.gates[{gk!r}].max_snooze_duration_hours must be a number when set."
                )
                return None, errors
            garc = gv.get("allowed_reason_codes")
            if garc is not None:
                if not isinstance(garc, list):
                    errors.append(
                        f"policy.gates[{gk!r}].allowed_reason_codes must be a list when set."
                    )
                    return None, errors
                for i, c in enumerate(garc):
                    if not isinstance(c, str) or not c.strip():
                        errors.append(
                            f"policy.gates[{gk!r}].allowed_reason_codes[{i}] must be a non-empty string."
                        )
                        return None, errors
            gsrf = gv.get("snooze_required_fields")
            if gsrf is not None:
                if not isinstance(gsrf, list):
                    errors.append(
                        f"policy.gates[{gk!r}].snooze_required_fields must be a list when set."
                    )
                    return None, errors
                for i, f in enumerate(gsrf):
                    if not isinstance(f, str) or not f.strip():
                        errors.append(
                            f"policy.gates[{gk!r}].snooze_required_fields[{i}] must be a non-empty string."
                        )
                        return None, errors
    return doc, errors


def effective_policy_for_gate(policy: dict[str, Any], gate: str) -> dict[str, Any]:
    eff: dict[str, Any] = {
        "max_snooze_duration_hours": policy.get("max_snooze_duration_hours"),
        "allowed_reason_codes": list(policy.get("allowed_reason_codes") or []),
        "snooze_required_fields": list(policy.get("snooze_required_fields") or []),
    }
    gates = policy.get("gates")
    if not isinstance(gates, dict):
        return eff
    overlay = gates.get(gate)
    if not isinstance(overlay, dict):
        return eff
    if "max_snooze_duration_hours" in overlay:
        eff["max_snooze_duration_hours"] = overlay["max_snooze_duration_hours"]
    if "allowed_reason_codes" in overlay:
        eff["allowed_reason_codes"] = list(overlay["allowed_reason_codes"] or [])
    if "snooze_required_fields" in overlay:
        eff["snooze_required_fields"] = list(overlay["snooze_required_fields"] or [])
    return eff


def _apply_policy_constraints(
    snooze: dict[str, Any],
    *,
    doc: dict[str, Any],
    gate: str,
    expires_at: datetime,
    now: datetime,
    policy: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    eff = effective_policy_for_gate(policy, gate)

    env = doc.get("environment")
    if env is None:
        errors.append(
            "exception.environment is required when policy-as-code validation is enabled "
            "(use 'production' for gate release, 'nonprod' for gate nonprod)."
        )
    elif env not in ("production", "nonprod"):
        errors.append("exception.environment must be 'production' or 'nonprod'.")
    else:
        expected = "production" if gate == "release" else "nonprod"
        if env != expected:
            errors.append(
                f"exception.environment {env!r} does not match gate {gate!r} "
                f"(expected {expected!r})."
            )

    max_h = eff.get("max_snooze_duration_hours")
    if max_h is not None:
        if not isinstance(max_h, (int, float)) or isinstance(max_h, bool):
            errors.append("policy max_snooze_duration_hours must be a number.")
        elif max_h <= 0:
            errors.append("policy max_snooze_duration_hours must be positive.")
        else:
            delta = expires_at - now
            if delta.total_seconds() > float(max_h) * 3600.0:
                errors.append(
                    f"snooze duration exceeds policy maximum of {max_h} hours "
                    f"(expires_at {expires_at.isoformat()} vs now {now.isoformat()})."
                )

    codes = eff.get("allowed_reason_codes") or []
    if codes:
        rc = snooze.get("reason_code")
        if not isinstance(rc, str) or not rc.strip():
            errors.append(
                "exception.snooze.reason_code is required when policy.allowed_reason_codes is non-empty."
            )
        elif rc.strip() not in codes:
            errors.append(
                f"exception.snooze.reason_code {rc.strip()!r} is not in policy allowed_reason_codes."
            )

    for field in eff.get("snooze_required_fields") or []:
        fn = field.strip()
        if fn not in snooze:
            errors.append(f"exception.snooze.{fn} is required by policy (snooze_required_fields).")
            continue
        val = snooze[fn]
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(
                f"exception.snooze.{fn} must be non-empty (required by policy)."
            )

    return errors


def _load_exception_doc(
    *,
    gate: str,
    env_json_key: str,
    env_path_key: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Load exception JSON from env (secret) or repo-relative path (var). Returns (doc or None, errors)."""
    errors: list[str] = []
    raw_json = (os.environ.get(env_json_key) or "").strip()
    rel_path = (os.environ.get(env_path_key) or "").strip()
    workspace = (os.environ.get("GITHUB_WORKSPACE") or "").strip() or os.getcwd()

    if raw_json and rel_path:
        errors.append(
            f"Both {env_json_key} and {env_path_key} are set; use only one exception source."
        )
        return None, errors

    if raw_json:
        try:
            doc = json.loads(raw_json)
        except json.JSONDecodeError as e:
            errors.append(f"{env_json_key} is not valid JSON: {e}")
            return None, errors
        if not isinstance(doc, dict):
            errors.append(f"{env_json_key} must be a JSON object.")
            return None, errors
        return doc, errors

    if rel_path:
        p = Path(workspace) / rel_path
        if not p.is_file():
            errors.append(f"{env_path_key} points to missing file: {p}")
            return None, errors
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"Exception file {p} is not valid JSON: {e}")
            return None, errors
        if not isinstance(doc, dict):
            errors.append(f"Exception file {p} must contain a JSON object.")
            return None, errors
        return doc, errors

    return None, errors


def _validate_exception(
    doc: dict[str, Any],
    *,
    gate: str,
    github_repository: str,
    policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    extra = set(doc.keys()) - ALLOWED_TOP_KEYS
    if extra:
        errors.append(f"Unknown top-level keys in exception record: {sorted(extra)}")
        return None, errors

    ver = doc.get("schema_version")
    if ver != 1:
        errors.append(
            f"exception.schema_version must be 1, got {ver!r}."
        )
        return None, errors

    g = doc.get("gate")
    if g != gate:
        errors.append(
            f"exception.gate must be {gate!r} for this workflow (got {g!r})."
        )
        return None, errors

    repo_needle = doc.get("github_repository")
    if repo_needle is not None:
        if not isinstance(repo_needle, str) or not repo_needle.strip():
            errors.append("exception.github_repository, if set, must be a non-empty string.")
            return None, errors
        if github_repository and repo_needle.strip() != github_repository:
            errors.append(
                f"exception.github_repository {repo_needle!r} does not match "
                f"GITHUB_REPOSITORY={github_repository!r}."
            )
            return None, errors

    snooze = doc.get("snooze")
    if not isinstance(snooze, dict):
        errors.append("exception.snooze must be a JSON object.")
        return None, errors

    allowed_snooze = frozenset(
        {"expires_at", "approver", "change_id", "reason", "reason_code"}
    )
    bad_snooze_keys = set(snooze.keys()) - allowed_snooze
    if bad_snooze_keys:
        errors.append(f"Unknown keys in exception.snooze: {sorted(bad_snooze_keys)}")
        return None, errors

    for req in ("expires_at", "approver", "change_id"):
        if req not in snooze:
            errors.append(f"exception.snooze.{req} is required.")
            return None, errors

    expires_raw = snooze["expires_at"]
    if not isinstance(expires_raw, str) or not expires_raw.strip():
        errors.append("exception.snooze.expires_at must be a non-empty ISO-8601 string.")
        return None, errors
    try:
        expires_at = _parse_iso_utc(expires_raw)
    except (ValueError, TypeError) as e:
        errors.append(f"exception.snooze.expires_at is not a valid ISO-8601 timestamp: {e}")
        return None, errors

    now = _utc_now()
    if expires_at <= now:
        errors.append(
            f"exception.snooze.expires_at is expired ({expires_at.isoformat()} <= {now.isoformat()})."
        )
        return None, errors

    approver = snooze["approver"]
    if not isinstance(approver, str) or not approver.strip():
        errors.append("exception.snooze.approver must be a non-empty string.")
        return None, errors

    change_id = snooze["change_id"]
    if not isinstance(change_id, str) or not change_id.strip():
        errors.append("exception.snooze.change_id must be a non-empty string.")
        return None, errors

    reason = snooze.get("reason")
    if reason is not None and not isinstance(reason, str):
        errors.append("exception.snooze.reason, if set, must be a string.")
        return None, errors

    reason_code = snooze.get("reason_code")
    if reason_code is not None and not isinstance(reason_code, str):
        errors.append("exception.snooze.reason_code, if set, must be a string.")
        return None, errors

    if policy is not None:
        errors.extend(
            _apply_policy_constraints(
                snooze,
                doc=doc,
                gate=gate,
                expires_at=expires_at,
                now=now,
                policy=policy,
            )
        )
        if errors:
            return None, errors

    env_val = doc.get("environment")
    env_out: str | None
    if isinstance(env_val, str) and env_val.strip():
        env_out = env_val.strip()
    else:
        env_out = None

    trace = {
        "schema_version": 1,
        "gate": gate,
        "snooze_expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "approver": approver.strip(),
        "change_id": change_id.strip(),
        "reason": reason.strip() if isinstance(reason, str) else None,
        "reason_code": reason_code.strip() if isinstance(reason_code, str) else None,
        "environment": env_out,
        "policy_applied": policy is not None,
        "evaluated_at_utc": now.isoformat().replace("+00:00", "Z"),
    }
    return trace, errors


def evaluate(
    state: dict[str, Any],
    *,
    gate: str,
    env_json_key: str,
    env_path_key: str,
    github_repository: str,
    policy: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any] | None, list[str]]:
    """Returns (decision allow|deny, reason_code, exception_trace|None, errors_for_deny)."""
    errors: list[str] = []

    if not state.get("evidence_found"):
        errors.append("live_drill.evidence_found is false; snooze cannot waive missing evidence.")
        return "deny", "missing_evidence", None, errors

    stale = bool(state.get("stale"))
    if not stale:
        return "allow", "fresh_within_window", None, []

    doc, load_errs = _load_exception_doc(
        gate=gate, env_json_key=env_json_key, env_path_key=env_path_key
    )
    errors.extend(load_errs)
    if load_errs:
        return "deny", "exception_load_error", None, errors

    if doc is None:
        errors.append(
            "Live-drill evidence is stale and no exception JSON is configured "
            f"({env_json_key} / {env_path_key})."
        )
        return "deny", "stale_no_exception", None, errors

    trace, val_errs = _validate_exception(
        doc, gate=gate, github_repository=github_repository, policy=policy
    )
    errors.extend(val_errs)
    if val_errs or trace is None:
        return "deny", "exception_invalid_or_expired", None, errors

    return "allow", "snooze_active", trace, []


def _main_approve_artifact(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Validate exception JSON against policy and write approval artifact (local only)."
    )
    p.add_argument(
        "--approve-artifact-out",
        required=True,
        type=Path,
        help="Write approved exception artifact JSON here.",
    )
    p.add_argument(
        "--exception-path",
        required=True,
        type=Path,
        help="Path to proposed exception JSON.",
    )
    p.add_argument(
        "--policy",
        required=True,
        type=Path,
        help="Path to policy-as-code JSON.",
    )
    p.add_argument(
        "--approval-environment",
        required=True,
        choices=("production", "nonprod"),
        help="GitHub workflow environment scope (production→release gate, nonprod→nonprod gate).",
    )
    args = p.parse_args(argv)

    gate = "release" if args.approval_environment == "production" else "nonprod"
    github_repository = (os.environ.get("GITHUB_REPOSITORY") or "").strip()

    policy, perrs = _load_policy_file(args.policy)
    if perrs or policy is None:
        for e in perrs:
            print(e, file=sys.stderr)
        return 1

    try:
        raw_ex = args.exception_path.read_text(encoding="utf-8")
        doc = json.loads(raw_ex)
    except FileNotFoundError:
        print(f"Exception file not found: {args.exception_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Exception file is not valid JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        print("Exception root must be a JSON object.", file=sys.stderr)
        return 1

    trace, errs = _validate_exception(
        doc, gate=gate, github_repository=github_repository, policy=policy
    )
    if errs or trace is None:
        print("Exception validation failed:", file=sys.stderr)
        for e in errs:
            print(f"- {e}", file=sys.stderr)
        return 1

    now = _utc_now()
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "oncall_evidence_exception_approval",
        "approved_at_utc": now.isoformat().replace("+00:00", "Z"),
        "approval_environment": args.approval_environment,
        "approval_gate": gate,
        "exception": doc,
        "validation_trace": trace,
    }
    for env_key, out_key in (
        ("GITHUB_REPOSITORY", "github_repository"),
        ("GITHUB_ACTOR", "github_actor"),
        ("GITHUB_RUN_ID", "github_run_id"),
        ("GITHUB_SHA", "github_sha"),
    ):
        v = (os.environ.get(env_key) or "").strip()
        if v:
            artifact[out_key] = v

    _write_decision(args.approve_artifact_out, artifact)
    print(
        f"Approved exception artifact written to {args.approve_artifact_out} "
        f"(environment={args.approval_environment}, gate={gate})."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--approve-artifact-out" in argv:
        return _main_approve_artifact(argv)

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--state",
        required=True,
        type=Path,
        help="Path to live-drill-evidence-state.json",
    )
    p.add_argument(
        "--decision-out",
        required=True,
        type=Path,
        help="Write gate decision JSON here (always, including on deny).",
    )
    p.add_argument(
        "--gate",
        required=True,
        choices=("release", "nonprod"),
        help="Which gate this run enforces (must match exception.gate).",
    )
    p.add_argument(
        "--policy",
        required=False,
        type=Path,
        default=None,
        help="Optional policy-as-code JSON (stricter validation: environment, duration, reason codes).",
    )
    args = p.parse_args(argv)

    gate = args.gate
    if gate == "release":
        env_json_key = "ONCALL_EVIDENCE_EXCEPTION_JSON"
        env_path_key = "ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH"
    else:
        env_json_key = "NONPROD_ONCALL_EVIDENCE_EXCEPTION_JSON"
        env_path_key = "NONPROD_ONCALL_EVIDENCE_EXCEPTION_RELATIVE_PATH"

    github_repository = (os.environ.get("GITHUB_REPOSITORY") or "").strip()

    policy: dict[str, Any] | None = None
    if args.policy is not None:
        policy, perrs = _load_policy_file(args.policy)
        if perrs or policy is None:
            payload = {
                "decision": "deny",
                "reason_code": "policy_load_error",
                "gate": gate,
                "live_drill": None,
                "exception_applied": False,
                "exception_trace": None,
                "errors": perrs,
            }
            _write_decision(args.decision_out, payload)
            print("\n".join(perrs), file=sys.stderr)
            return 1

    try:
        raw_state = args.state.read_text(encoding="utf-8")
        state = json.loads(raw_state)
    except FileNotFoundError:
        payload = {
            "decision": "deny",
            "reason_code": "state_missing",
            "gate": gate,
            "live_drill": None,
            "exception_trace": None,
            "errors": [f"State file not found: {args.state}"],
        }
        _write_decision(args.decision_out, payload)
        print("\n".join(payload["errors"]), file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        payload = {
            "decision": "deny",
            "reason_code": "state_malformed",
            "gate": gate,
            "live_drill": None,
            "exception_trace": None,
            "errors": [f"State file is not valid JSON: {e}"],
        }
        _write_decision(args.decision_out, payload)
        print("\n".join(payload["errors"]), file=sys.stderr)
        return 1

    if not isinstance(state, dict):
        payload = {
            "decision": "deny",
            "reason_code": "state_malformed",
            "gate": gate,
            "live_drill": None,
            "exception_trace": None,
            "errors": ["State root must be a JSON object."],
        }
        _write_decision(args.decision_out, payload)
        print("\n".join(payload["errors"]), file=sys.stderr)
        return 1

    decision, reason_code, exception_trace, errs = evaluate(
        state,
        gate=gate,
        env_json_key=env_json_key,
        env_path_key=env_path_key,
        github_repository=github_repository,
        policy=policy,
    )

    live_drill_trace = {
        "evidence_found": state.get("evidence_found"),
        "stale": state.get("stale"),
        "artifact_name": state.get("artifact_name"),
        "workflow_file": state.get("workflow_file"),
        "branch": state.get("branch"),
        "max_age_hours": state.get("max_age_hours"),
        "evidence_created_at": state.get("evidence_created_at"),
        "age_seconds": state.get("age_seconds"),
        "workflow_run_id": state.get("workflow_run_id"),
        "workflow_run_url": state.get("workflow_run_url"),
    }

    payload: dict[str, Any] = {
        "decision": decision,
        "reason_code": reason_code,
        "gate": gate,
        "live_drill": live_drill_trace,
        "exception_applied": exception_trace is not None,
        "exception_trace": exception_trace,
        "errors": errs,
    }

    _write_decision(args.decision_out, payload)

    if decision == "allow":
        msg = (
            f"On-call evidence gate ALLOW ({reason_code}):\n"
            f"- workflow_run_url: {live_drill_trace.get('workflow_run_url')}\n"
            f"- stale: {live_drill_trace.get('stale')}\n"
        )
        if exception_trace:
            msg += (
                f"- snooze change_id: {exception_trace.get('change_id')}\n"
                f"- approver: {exception_trace.get('approver')}\n"
                f"- snooze_expires_at: {exception_trace.get('snooze_expires_at')}\n"
            )
        print(msg)
        return 0

    print(
        "On-call evidence gate DENY:\n"
        f"- reason_code: {reason_code}\n"
        + "\n".join(f"- {e}" for e in errs),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
