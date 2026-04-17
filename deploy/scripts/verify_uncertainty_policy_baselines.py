#!/usr/bin/env python3
"""Verify uncertainty governance policy bytes against per-environment baseline SHA256 pins.

Reads only local JSON files (no network). Emits a single JSON object on stdout and optional
--json-out path. Exit status is 0 when all environments match and the baseline document is
valid; nonzero on validation failure or policy drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASELINE_SCHEMA_VERSIONS = frozenset({1})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_iso_date(value: Any) -> date | None:
    """Parse YYYY-MM-DD or ISO8601 datetime to UTC calendar date."""
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_baseline_env_entry(env_name: str, entry: Any) -> list[str]:
    errors: list[str] = []
    prefix = f"environments.{env_name}"
    if not isinstance(entry, dict):
        return [f"{prefix}: must be an object"]

    exp = entry.get("expected_policy_sha256")
    if not _is_non_empty_str(exp):
        errors.append(f"{prefix}.expected_policy_sha256: required 64-char lowercase hex string")
    else:
        low = str(exp).strip().lower()
        if not SHA256_RE.match(low):
            errors.append(
                f"{prefix}.expected_policy_sha256: must be 64 lowercase hexadecimal characters"
            )

    own = entry.get("ownership")
    if not isinstance(own, dict):
        errors.append(f"{prefix}.ownership: required object")
    else:
        if not _is_non_empty_str(own.get("owning_team")):
            errors.append(f"{prefix}.ownership.owning_team: required non-empty string")
        if not _is_non_empty_str(own.get("policy_owner")):
            errors.append(f"{prefix}.ownership.policy_owner: required non-empty string")

    cr = entry.get("change_review")
    if not isinstance(cr, dict):
        errors.append(f"{prefix}.change_review: required object")
    else:
        approved = cr.get("last_baseline_approved_at")
        if not _is_non_empty_str(approved):
            errors.append(
                f"{prefix}.change_review.last_baseline_approved_at: "
                "required non-empty string (ISO date)"
            )
        elif parse_iso_date(approved) is None:
            errors.append(
                f"{prefix}.change_review.last_baseline_approved_at: "
                "must be YYYY-MM-DD or ISO8601 datetime"
            )
        if not _is_non_empty_str(cr.get("approval_change_id")):
            errors.append(
                f"{prefix}.change_review.approval_change_id: required non-empty string "
                "(ticket / change record id)"
            )

    return errors


def validate_baseline_root(doc: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ver = doc.get("schema_version")
    if ver is None:
        errors.append("schema_version: required")
    elif ver not in BASELINE_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version: unsupported {ver!r}; supported {sorted(BASELINE_SCHEMA_VERSIONS)}"
        )

    pol = doc.get("policy_path")
    if not _is_non_empty_str(pol):
        errors.append("policy_path: required non-empty string (repo-relative path)")
    elif ".." in str(pol) or str(pol).startswith("/"):
        errors.append("policy_path: must be repo-relative without '..' or absolute paths")

    envs = doc.get("environments")
    if envs is None:
        errors.append("environments: required object")
    elif not isinstance(envs, dict):
        errors.append("environments: must be an object mapping environment name to baseline row")
    elif len(envs) == 0:
        errors.append("environments: must contain at least one environment")
    else:
        for name, entry in envs.items():
            if not _is_non_empty_str(name):
                errors.append("environments: invalid environment key (non-empty string required)")
                continue
            errors.extend(validate_baseline_env_entry(str(name).strip(), entry))

    return errors


def load_json_object(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("root must be a JSON object")
    return data


def find_repo_root(start: Path) -> Path:
    """Ascend from start until data/governed/uncertainty_governance_policy.json exists."""
    cur = start.resolve()
    marker = Path("data") / "governed" / "uncertainty_governance_policy.json"
    for p in [cur, *cur.parents]:
        if (p / marker).is_file():
            return p
    return Path.cwd().resolve()


def resolve_policy_path(baselines_path: Path, doc: dict[str, Any]) -> Path:
    rel = str(doc["policy_path"]).strip()
    repo = find_repo_root(baselines_path)
    candidate = (repo / rel).resolve()
    cwd_guess = (Path.cwd() / rel).resolve()
    if cwd_guess.is_file():
        return cwd_guess
    return candidate


def verify_baselines(
    baselines_path: Path,
    policy_path: Path | None,
    doc: dict[str, Any],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Return (errors, warnings, per_environment rows)."""
    errors: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []

    if policy_path is None:
        policy_path = resolve_policy_path(baselines_path, doc)

    if not policy_path.is_file():
        errors.append(f"policy file not found: {policy_path}")
        return errors, warnings, {"environments": rows, "actual_policy_sha256": None}

    actual = sha256_file(policy_path)
    envs = doc.get("environments")
    if not isinstance(envs, dict):
        return errors, warnings, {"environments": rows, "actual_policy_sha256": actual}

    for env_name in sorted(envs.keys()):
        entry = envs[env_name]
        row: dict[str, Any] = {
            "environment": env_name,
            "expected_policy_sha256": None,
            "actual_policy_sha256": actual,
            "drift": False,
            "outcome": "skipped",
        }
        if isinstance(entry, dict):
            exp_raw = entry.get("expected_policy_sha256")
            if _is_non_empty_str(exp_raw):
                exp = str(exp_raw).strip().lower()
                row["expected_policy_sha256"] = exp
                if exp == actual:
                    row["outcome"] = "match"
                else:
                    row["drift"] = True
                    row["outcome"] = "drift"
                    errors.append(
                        f"environment {env_name!r}: policy SHA256 drift "
                        f"(actual={actual}, expected={exp})"
                    )
            row["ownership"] = entry.get("ownership")
            row["change_review"] = entry.get("change_review")
        rows.append(row)

    return errors, warnings, {"environments": rows, "actual_policy_sha256": actual}


def build_result(
    *,
    ok: bool,
    baselines_path: str,
    policy_path: str | None,
    validation_errors: list[str],
    verification_errors: list[str],
    warnings: list[str],
    drift_detected: bool,
    detail: dict[str, Any],
) -> dict[str, Any]:
    all_err = list(validation_errors) + list(verification_errors)
    return {
        "ok": ok,
        "alert": "drift" if drift_detected else ("invalid_baseline" if validation_errors else None),
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "baselines_path": baselines_path,
        "policy_path": policy_path,
        "error_count": len(all_err),
        "warning_count": len(warnings),
        "errors": all_err,
        "warnings": warnings,
        "drift_detected": drift_detected,
        "actual_policy_sha256": detail.get("actual_policy_sha256"),
        "environments": detail.get("environments", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_baselines = (
        Path(__file__).resolve().parents[1] / "ops" / "uncertainty-governance-policy-baselines.example.json"
    )
    parser.add_argument(
        "--baselines",
        type=Path,
        default=default_baselines,
        help=f"Path to governance policy baselines JSON (default: {default_baselines})",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="Override policy file path (default: policy_path from baselines, resolved from repo root)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write verification result JSON to this path",
    )
    parser.add_argument(
        "--reject-example-baseline-path",
        action="store_true",
        help=(
            "Exit nonzero if --baselines resolves to uncertainty-governance-policy-baselines.example.json "
            "(use in CI with private baseline artifacts only)"
        ),
    )
    args = parser.parse_args()

    baselines_path = args.baselines.resolve()
    example_name = "uncertainty-governance-policy-baselines.example.json"
    if args.reject_example_baseline_path and baselines_path.name == example_name:
        result = build_result(
            ok=False,
            baselines_path=str(baselines_path),
            policy_path=str(args.policy.resolve()) if args.policy else None,
            validation_errors=[
                f"baselines path must not be the committed example ({example_name}); "
                "use a private baselines JSON file locally, or in hosted CI the GitHub Environment "
                "'uncertainty-governance-baseline-verification' secret "
                "UNCERTAINTY_GOVERNANCE_POLICY_BASELINES_JSON"
            ],
            verification_errors=[],
            warnings=[],
            drift_detected=False,
            detail={"environments": [], "actual_policy_sha256": None},
        )
        print(json.dumps(result, indent=2))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1
    policy_override = args.policy.resolve() if args.policy else None

    try:
        doc = load_json_object(baselines_path)
    except FileNotFoundError:
        result = build_result(
            ok=False,
            baselines_path=str(baselines_path),
            policy_path=str(policy_override) if policy_override else None,
            validation_errors=[f"baselines file not found: {baselines_path}"],
            verification_errors=[],
            warnings=[],
            drift_detected=False,
            detail={"environments": [], "actual_policy_sha256": None},
        )
        print(json.dumps(result, indent=2))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        result = build_result(
            ok=False,
            baselines_path=str(baselines_path),
            policy_path=str(policy_override) if policy_override else None,
            validation_errors=[f"invalid baselines JSON: {e}"],
            verification_errors=[],
            warnings=[],
            drift_detected=False,
            detail={"environments": [], "actual_policy_sha256": None},
        )
        print(json.dumps(result, indent=2))
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    val_err = validate_baseline_root(doc)
    drift_detected = False
    ver_err: list[str] = []
    warn: list[str] = []
    detail: dict[str, Any] = {"environments": [], "actual_policy_sha256": None}

    resolved_policy: Path | None = policy_override
    if not val_err:
        if resolved_policy is None:
            resolved_policy = resolve_policy_path(baselines_path, doc)
        ver_err, warn, detail = verify_baselines(baselines_path, resolved_policy, doc)
        drift_detected = any(
            isinstance(r, dict) and r.get("drift") for r in detail.get("environments", [])
        )

    ok = len(val_err) == 0 and len(ver_err) == 0
    result = build_result(
        ok=ok,
        baselines_path=str(baselines_path),
        policy_path=str(resolved_policy) if resolved_policy else None,
        validation_errors=val_err,
        verification_errors=ver_err,
        warnings=warn,
        drift_detected=drift_detected,
        detail=detail,
    )

    print(json.dumps(result, indent=2))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
