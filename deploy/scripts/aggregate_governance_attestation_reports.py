#!/usr/bin/env python3
"""Merge governance attestation aging JSON reports into a trend / SLA aggregate.

Reads only local JSON files (no network). Expects inputs produced by
`report_governance_attestation_age.py report` (report_kind: governance_baseline_approval_age).

Deterministic output: sorted keys, stable ordering of repositories and environments.
"""

from __future__ import annotations

import argparse
import glob as glob_mod
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

EXPECTED_KIND = "governance_baseline_approval_age"
SLA_EVAL_KIND = "governance_attestation_sla_evaluation"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_report(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


def _status_bucket(status: str) -> str:
    s = (status or "").strip()
    if s in ("ok", "reminder", "stale"):
        return s
    if s == "invalid_future_approval_date":
        return "invalid_future"
    if s == "missing_or_unparseable_date":
        return "missing_or_unparseable"
    if s == "unknown":
        return "unknown"
    return "other"


def _parse_age_days(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_reported_at(value: Any) -> tuple[str, float]:
    """Return (raw string, sort key). Missing/invalid sorts early."""
    if not isinstance(value, str) or not value.strip():
        return "", 0.0
    raw = value.strip()
    try:
        if raw.endswith("Z"):
            raw_z = raw[:-1] + "+00:00"
        else:
            raw_z = raw
        dt = datetime.fromisoformat(raw_z.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return value.strip(), dt.timestamp()
    except ValueError:
        return value.strip(), 0.0


def _load_sla_policy(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("SLA policy root must be a JSON object")
    return doc


def _merged_rule_for_environment(policy: Mapping[str, Any], env_name: str) -> dict[str, Any]:
    defaults = policy.get("defaults")
    base: dict[str, Any] = dict(defaults) if isinstance(defaults, dict) else {}
    envs = policy.get("environments")
    if isinstance(envs, dict):
        spec = envs.get(env_name)
        if isinstance(spec, dict):
            return {**base, **spec}
    return base


def _normalize_allowed_statuses(rule: Mapping[str, Any]) -> list[str] | None:
    raw = rule.get("allowed_statuses")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for x in raw:
        if isinstance(x, str) and x.strip():
            out.append(x.strip().lower())
    return out


def evaluate_environment_sla(
    latest_by_repo: Mapping[str, Mapping[str, Any]],
    policy: Mapping[str, Any],
    *,
    policy_source: str,
) -> dict[str, Any]:
    """Evaluate latest per-repo environment rows against optional per-environment SLA rules."""
    breaches: list[dict[str, Any]] = []
    evaluated_env_rows = 0
    skipped_no_rule = 0

    for repo in sorted(latest_by_repo.keys()):
        snap = latest_by_repo[repo]
        for row in snap.get("environments") or []:
            if not isinstance(row, dict):
                continue
            env_name = str(row.get("environment", "")).strip() or "unknown"
            rule = _merged_rule_for_environment(policy, env_name)
            allowed = _normalize_allowed_statuses(rule)
            max_age = rule.get("max_age_days")

            has_status_rule = allowed is not None and len(allowed) > 0
            has_age_rule = max_age is not None and isinstance(max_age, (int, float)) and not isinstance(max_age, bool)

            if not has_status_rule and not has_age_rule:
                skipped_no_rule += 1
                continue

            evaluated_env_rows += 1
            st_raw = str(row.get("status", "")).strip()
            st_cmp = st_raw.lower()
            age_val = _parse_age_days(row.get("age_days"))

            reasons: list[str] = []
            if has_status_rule and st_cmp not in (allowed or []):
                reasons.append("status_not_allowed")
            if has_age_rule:
                lim = float(max_age)  # type: ignore[arg-type]
                if age_val is None:
                    reasons.append("age_missing_for_max_check")
                elif age_val > lim:
                    reasons.append("age_exceeds_max")

            if reasons:
                breaches.append(
                    {
                        "source_repository": repo,
                        "environment": env_name,
                        "status": st_raw,
                        "age_days": row.get("age_days"),
                        "breach_reasons": sorted(reasons),
                        "rule_snapshot": {
                            "allowed_statuses": allowed if has_status_rule else None,
                            "max_age_days": float(max_age) if has_age_rule else None,
                        },
                    }
                )

    repos_hit = sorted({b["source_repository"] for b in breaches})
    return {
        "schema_version": 1,
        "report_kind": SLA_EVAL_KIND,
        "evaluated_at": _utc_now_iso(),
        "policy_source": policy_source,
        "compliant": len(breaches) == 0,
        "summary": {
            "breach_count": len(breaches),
            "repositories_with_breaches": len(repos_hit),
            "evaluated_environment_rows": evaluated_env_rows,
            "skipped_environment_rows_no_applicable_rule": skipped_no_rule,
        },
        "breaches": sorted(
            breaches,
            key=lambda b: (str(b.get("source_repository", "")), str(b.get("environment", "")), str(b.get("status", ""))),
        ),
    }


def aggregate_reports(
    paths: list[Path],
    *,
    sla_policy: dict[str, Any] | None = None,
    sla_policy_source: str | None = None,
) -> dict[str, Any]:
    inputs_meta: list[dict[str, Any]] = []
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)

    warnings: list[str] = []
    for p in sorted(paths, key=lambda x: str(x)):
        try:
            doc = _load_report(p)
        except (OSError, json.JSONDecodeError) as e:
            warnings.append(f"{p}: skip (read/JSON error: {e})")
            continue

        kind = doc.get("report_kind")
        if kind != EXPECTED_KIND:
            warnings.append(f"{p}: skip (report_kind={kind!r}, expected {EXPECTED_KIND!r})")
            continue

        repo = doc.get("source_repository")
        if not isinstance(repo, str) or not repo.strip():
            repo = "unknown"
        else:
            repo = repo.strip()

        raw_ts, ts = _parse_reported_at(doc.get("reported_at"))
        entry = {
            "input_path": str(p),
            "source_repository": repo,
            "reported_at": raw_ts,
            "_sort_ts": ts,
            "ok": bool(doc.get("ok")),
            "as_of_utc_date": doc.get("as_of_utc_date"),
            "max_age_days": doc.get("max_age_days"),
            "reminder_days_before_max": doc.get("reminder_days_before_max"),
            "environments": doc.get("environments") if isinstance(doc.get("environments"), list) else [],
        }
        inputs_meta.append({k: v for k, v in entry.items() if k != "_sort_ts"})
        by_repo[repo].append(entry)

    # Latest snapshot per repository (by reported_at)
    latest_by_repo: dict[str, dict[str, Any]] = {}
    for repo, reps in by_repo.items():
        best = max(reps, key=lambda r: r["_sort_ts"])
        latest_by_repo[repo] = {k: v for k, v in best.items() if k != "_sort_ts"}

    # Per-repo environment rows from latest snapshot only
    stale_by_repo_env: dict[str, dict[str, int]] = {}
    status_totals_latest: dict[str, int] = defaultdict(int)

    for repo in sorted(latest_by_repo.keys()):
        snap = latest_by_repo[repo]
        env_map: dict[str, int] = {}
        for row in snap.get("environments") or []:
            if not isinstance(row, dict):
                continue
            env_name = str(row.get("environment", "")).strip() or "unknown"
            st = str(row.get("status", "")).strip()
            bucket = _status_bucket(st)
            status_totals_latest[bucket] += 1
            if bucket == "stale":
                env_map[env_name] = env_map.get(env_name, 0) + 1
        stale_by_repo_env[repo] = env_map

    stale_repo_counts = {r: sum(stale_by_repo_env[r].values()) for r in sorted(stale_by_repo_env.keys())}
    stale_env_counts_flat: dict[str, int] = {}
    for r, em in stale_by_repo_env.items():
        for e, c in em.items():
            key = f"{r}::{e}"
            stale_env_counts_flat[key] = c

    max_age_refs = sorted(
        {m.get("max_age_days") for m in latest_by_repo.values() if m.get("max_age_days") is not None},
        key=lambda x: str(x),
    )

    out: dict[str, Any] = {
        "schema_version": 1,
        "report_kind": "governance_attestation_trend_aggregate",
        "aggregated_at": _utc_now_iso(),
        "inputs_count": len(paths),
        "inputs_used": len(inputs_meta),
        "inputs": sorted(inputs_meta, key=lambda x: (x.get("source_repository", ""), x.get("reported_at", ""))),
        "latest_snapshot_by_repository": {k: latest_by_repo[k] for k in sorted(latest_by_repo.keys())},
        "sla_summary": {
            "description": "Counts from the latest report per source_repository only.",
            "status_counts": {k: status_totals_latest[k] for k in sorted(status_totals_latest.keys())},
            "stale_count_total": sum(stale_repo_counts.values()),
            "stale_counts_by_repository": stale_repo_counts,
            "stale_counts_by_repository_environment": stale_env_counts_flat,
            "max_age_days_values_seen_in_latest": max_age_refs,
        },
        "trend": {
            "description": "All input snapshots grouped by repository (for cross-run comparison).",
            "snapshots_by_repository": {
                r: sorted([{k: v for k, v in x.items() if k != "_sort_ts"} for x in reps], key=lambda z: z.get("reported_at", ""))
                for r, reps in sorted(by_repo.items(), key=lambda t: t[0])
            },
        },
        "warnings": warnings,
    }

    if sla_policy is not None and sla_policy_source:
        out["sla_policy_evaluation"] = evaluate_environment_sla(
            latest_by_repo,
            sla_policy,
            policy_source=sla_policy_source,
        )
    return out


def _render_sla_markdown(evaluation: Mapping[str, Any]) -> str:
    summ = evaluation.get("summary") or {}
    lines = [
        "## Governance attestation SLA evaluation",
        "",
        f"- **evaluated_at**: `{evaluation.get('evaluated_at')}`",
        f"- **policy_source**: `{evaluation.get('policy_source')}`",
        f"- **compliant**: `{evaluation.get('compliant')}`",
        "",
        "### Summary",
        "",
        "| metric | value |",
        "|--------|-------|",
        f"| breach_count | `{summ.get('breach_count')}` |",
        f"| repositories_with_breaches | `{summ.get('repositories_with_breaches')}` |",
        f"| evaluated_environment_rows | `{summ.get('evaluated_environment_rows')}` |",
        f"| skipped_no_applicable_rule | `{summ.get('skipped_environment_rows_no_applicable_rule')}` |",
        "",
    ]
    breaches = evaluation.get("breaches") or []
    if breaches:
        lines.extend(
            [
                "### Breaches",
                "",
                "| repository | environment | status | age_days | reasons |",
                "|------------|-------------|--------|----------|---------|",
            ]
        )
        for b in breaches:
            if not isinstance(b, dict):
                continue
            rs = b.get("breach_reasons") or []
            rs_s = ", ".join(str(x) for x in rs) if isinstance(rs, list) else str(rs)
            lines.append(
                f"| `{b.get('source_repository','')}` | `{b.get('environment','')}` | "
                f"`{b.get('status','')}` | `{b.get('age_days')}` | `{rs_s}` |"
            )
    lines.append("")
    return "\n".join(lines)


def _render_dashboard(data: dict[str, Any]) -> str:
    sla = data.get("sla_summary") or {}
    counts = sla.get("status_counts") or {}
    stale_by_repo = sla.get("stale_counts_by_repository") or {}
    lines = [
        "## Governance attestation trend aggregate",
        "",
        f"- **aggregated_at**: `{data.get('aggregated_at')}`",
        f"- **inputs_used** / **inputs_count**: `{data.get('inputs_used')}` / `{data.get('inputs_count')}`",
        "",
        "### SLA summary (latest snapshot per repository)",
        "",
        "| status bucket | count |",
        "|---------------|-------|",
    ]
    for k in sorted(counts.keys()):
        lines.append(f"| `{k}` | `{counts[k]}` |")
    lines.extend(
        [
            "",
            "### Stale environments (latest snapshot)",
            "",
            "| repository | stale row count |",
            "|------------|-----------------|",
        ]
    )
    for repo in sorted(stale_by_repo.keys()):
        lines.append(f"| `{repo}` | `{stale_by_repo[repo]}` |")
    lines.extend(["", "### Per-repository environment detail (latest)", ""])
    rows = []
    for repo in sorted((data.get("latest_snapshot_by_repository") or {}).keys()):
        snap = (data.get("latest_snapshot_by_repository") or {}).get(repo) or {}
        for env_row in snap.get("environments") or []:
            if not isinstance(env_row, dict):
                continue
            rows.append(
                (
                    repo,
                    str(env_row.get("environment", "")),
                    str(env_row.get("status", "")),
                    env_row.get("age_days"),
                    str(snap.get("reported_at", "")),
                )
            )
    rows.sort(key=lambda t: (t[0], t[1]))
    lines.extend(
        [
            "| repository | environment | status | age_days | snapshot reported_at |",
            "|------------|-------------|--------|----------|----------------------|",
        ]
    )
    for repo, env, st, age, rep_at in rows:
        lines.append(f"| `{repo}` | `{env}` | `{st}` | `{age}` | `{rep_at}` |")
    ev = data.get("sla_policy_evaluation")
    if isinstance(ev, dict):
        lines.extend(["", _render_sla_markdown(ev).strip(), ""])
    warn = data.get("warnings") or []
    if warn:
        lines.extend(["", "### Warnings", ""])
        for w in warn:
            lines.append(f"- {w}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        default=[],
        help="Path to a governance-attestation-aging-report.json (repeatable)",
    )
    parser.add_argument(
        "--input-glob",
        action="append",
        default=[],
        help="Glob of report JSON files (repeatable; expanded relative to cwd)",
    )
    parser.add_argument("--json-out", type=Path, required=True, help="Write aggregate JSON here")
    parser.add_argument("--markdown-out", type=Path, default=None, help="Optional Markdown dashboard path")
    parser.add_argument(
        "--sla-policy",
        type=Path,
        default=None,
        help="Optional JSON SLA policy (environment rules); no network, local file only",
    )
    parser.add_argument(
        "--sla-json-out",
        type=Path,
        default=None,
        help="Write SLA evaluation JSON here (default: sibling of --json-out named governance-attestation-sla-evaluation.json)",
    )
    parser.add_argument(
        "--sla-markdown-out",
        type=Path,
        default=None,
        help="Write SLA evaluation Markdown here (default: sibling of --json-out named governance-attestation-sla-evaluation.md)",
    )
    args = parser.parse_args()

    paths: list[Path] = []
    for p in args.inputs:
        paths.append(p.resolve())
    for pattern in args.input_glob:
        for g in sorted(glob_mod.glob(pattern, recursive=True)):
            paths.append(Path(g).resolve())

    # De-dupe while preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)

    if not uniq:
        print(json.dumps({"ok": False, "error": "no inputs: pass --input and/or --input-glob"}))
        return 1

    missing = [p for p in uniq if not p.is_file()]
    if missing:
        print(json.dumps({"ok": False, "error": "missing files", "missing": [str(x) for x in missing]}))
        return 1

    sla_policy_doc: dict[str, Any] | None = None
    sla_policy_source: str | None = None
    if args.sla_policy is not None:
        pol_path = Path(args.sla_policy).resolve()
        if not pol_path.is_file():
            print(json.dumps({"ok": False, "error": "sla policy file missing", "path": str(pol_path)}))
            return 1
        try:
            sla_policy_doc = _load_sla_policy(pol_path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(json.dumps({"ok": False, "error": f"sla policy invalid: {e}", "path": str(pol_path)}))
            return 1
        sla_policy_source = str(pol_path)

    data = aggregate_reports(uniq, sla_policy=sla_policy_doc, sla_policy_source=sla_policy_source)
    data["ok"] = True

    out_json = Path(args.json_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if sla_policy_doc is not None and sla_policy_source:
        sla_json = Path(args.sla_json_out) if args.sla_json_out else out_json.parent / "governance-attestation-sla-evaluation.json"
        sla_md = Path(args.sla_markdown_out) if args.sla_markdown_out else out_json.parent / "governance-attestation-sla-evaluation.md"
        ev = data.get("sla_policy_evaluation")
        if isinstance(ev, dict):
            sla_json.parent.mkdir(parents=True, exist_ok=True)
            sla_json.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            sla_md.write_text(_render_sla_markdown(ev), encoding="utf-8")

    if args.markdown_out:
        md = Path(args.markdown_out)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(_render_dashboard(data), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
