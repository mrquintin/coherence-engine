#!/usr/bin/env python3
"""Compare expected org repo enrollment vs governance trend aggregate inputs (local files only).

Reads ``governance-attestation-trend-aggregate.json`` (report_kind: governance_attestation_trend_aggregate)
and an expected repository list (newline-separated). Optional SLA exception manifest marks repos that
may be missing without failing compliance.

No network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


REPORT_KIND = "governance_enrollment_coverage"
AGGREGATE_KIND = "governance_attestation_trend_aggregate"


def _norm_repo(s: str) -> str:
    return s.strip().replace(" ", "")


def parse_repo_list(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        r = _norm_repo(line)
        if r and not r.startswith("#"):
            out.append(r)
    return sorted(set(out))


def load_exception_manifest(
    path: Path | None, raw_json: str | None
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if raw_json and raw_json.strip():
        try:
            doc = json.loads(raw_json)
        except json.JSONDecodeError as e:
            return {}, [f"exception manifest JSON invalid: {e}"]
        if not isinstance(doc, dict):
            return {}, ["exception manifest root must be object"]
        return doc, errors
    if path is not None and path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {}, [f"exception manifest read error: {e}"]
        if not isinstance(doc, dict):
            return {}, ["exception manifest root must be object"]
        return doc, errors
    return {}, errors


def allowed_missing_from_manifest(manifest: dict[str, Any]) -> set[str]:
    raw = manifest.get("allowed_missing_repositories")
    if not isinstance(raw, list):
        return set()
    return {_norm_repo(str(x)) for x in raw if isinstance(x, str) and _norm_repo(str(x))}


def observed_repositories(aggregate: dict[str, Any]) -> set[str]:
    latest = aggregate.get("latest_snapshot_by_repository")
    if isinstance(latest, dict):
        return {_norm_repo(k) for k in latest.keys() if _norm_repo(str(k))}
    repos: set[str] = set()
    for row in aggregate.get("inputs") or []:
        if not isinstance(row, dict):
            continue
        sr = row.get("source_repository")
        if isinstance(sr, str) and _norm_repo(sr):
            repos.add(_norm_repo(sr))
    return repos


def build_report(
    *,
    expected: list[str],
    aggregate: dict[str, Any],
    manifest: dict[str, Any],
    manifest_errors: list[str],
) -> dict[str, Any]:
    exp_set = set(expected)
    obs = observed_repositories(aggregate)
    allowed_miss = allowed_missing_from_manifest(manifest)
    missing = sorted(exp_set - obs - allowed_miss)
    unexpected = sorted(obs - exp_set)
    allowed_missing_hit = sorted((exp_set - obs) & allowed_miss)
    compliant = len(missing) == 0 and len(manifest_errors) == 0

    return {
        "schema_version": 1,
        "report_kind": REPORT_KIND,
        "compliant": compliant,
        "summary": {
            "expected_count": len(exp_set),
            "observed_count": len(obs),
            "missing_count": len(missing),
            "unexpected_observed_count": len(unexpected),
            "allowed_missing_applied_count": len(allowed_missing_hit),
        },
        "expected_repositories": sorted(exp_set),
        "observed_repositories": sorted(obs),
        "missing_repositories": missing,
        "unexpected_observed_repositories": unexpected,
        "allowed_missing_repositories_effective": allowed_missing_hit,
        "manifest_errors": manifest_errors,
        "aggregate_report_kind": aggregate.get("report_kind"),
        "aggregate_aggregated_at": aggregate.get("aggregated_at"),
    }


def render_markdown(rep: dict[str, Any]) -> str:
    s = rep.get("summary") or {}
    lines = [
        "## Governance trend enrollment coverage",
        "",
        f"- **compliant**: `{rep.get('compliant')}`",
        f"- **expected_count**: `{s.get('expected_count')}`",
        f"- **observed_count**: `{s.get('observed_count')}`",
        f"- **missing_count**: `{s.get('missing_count')}`",
        f"- **unexpected_observed_count**: `{s.get('unexpected_observed_count')}`",
        "",
    ]
    miss = rep.get("missing_repositories") or []
    if miss:
        lines.extend(["### Missing (not in aggregate, not excepted)", ""])
        for r in miss:
            lines.append(f"- `{r}`")
        lines.append("")
    um = rep.get("unexpected_observed_repositories") or []
    if um:
        lines.extend(["### Observed but not in expected list", ""])
        for r in um:
            lines.append(f"- `{r}`")
        lines.append("")
    me = rep.get("manifest_errors") or []
    if me:
        lines.extend(["### Manifest errors", ""])
        for e in me:
            lines.append(f"- {e}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--aggregate-json",
        type=Path,
        required=True,
        help="Path to governance-attestation-trend-aggregate.json",
    )
    p.add_argument(
        "--expected-repos-file",
        type=Path,
        default=None,
        help="Newline-separated owner/repo list (optional if GOVERNANCE_ENROLLMENT_EXPECTED_REPO_LIST set)",
    )
    p.add_argument(
        "--exception-manifest",
        type=Path,
        default=None,
        help="Optional JSON path (allowed_missing_repositories); optional env override below",
    )
    p.add_argument("--json-out", type=Path, required=True)
    p.add_argument("--markdown-out", type=Path, default=None)
    p.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Exit 2 when an expected repo list is configured and coverage is not compliant",
    )
    args = p.parse_args()

    agg_path = args.aggregate_json.resolve()
    if not agg_path.is_file():
        print(json.dumps({"ok": False, "error": f"aggregate missing: {agg_path}"}))
        return 1
    try:
        aggregate = json.loads(agg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"aggregate invalid JSON: {e}"}))
        return 1
    if not isinstance(aggregate, dict):
        print(json.dumps({"ok": False, "error": "aggregate root must be object"}))
        return 1
    if aggregate.get("report_kind") != AGGREGATE_KIND:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"expected report_kind {AGGREGATE_KIND!r}, got {aggregate.get('report_kind')!r}",
                }
            )
        )
        return 1

    raw_list = (os.environ.get("GOVERNANCE_ENROLLMENT_EXPECTED_REPO_LIST") or "").strip()
    if args.expected_repos_file is not None and args.expected_repos_file.is_file():
        raw_list = args.expected_repos_file.read_text(encoding="utf-8")
    expected = parse_repo_list(raw_list)

    ex_secret = (os.environ.get("GOVERNANCE_ENROLLMENT_SLA_EXCEPTION_MANIFEST_JSON") or "").strip()
    manifest, merr = load_exception_manifest(args.exception_manifest, ex_secret or None)
    allowed_mkeys = frozenset(
        {
            "schema_version",
            "description",
            "allowed_missing_repositories",
            "reason_by_repository",
        }
    )
    bad_mkeys = set(manifest.keys()) - allowed_mkeys
    if bad_mkeys:
        merr.append(f"unknown top-level keys in manifest: {sorted(bad_mkeys)}")

    if not expected:
        rep = {
            "schema_version": 1,
            "report_kind": REPORT_KIND,
            "compliant": True,
            "summary": {
                "expected_count": 0,
                "observed_count": len(observed_repositories(aggregate)),
                "missing_count": 0,
                "unexpected_observed_count": 0,
                "allowed_missing_applied_count": 0,
            },
            "expected_repositories": [],
            "observed_repositories": sorted(observed_repositories(aggregate)),
            "missing_repositories": [],
            "unexpected_observed_repositories": [],
            "allowed_missing_repositories_effective": [],
            "manifest_errors": merr,
            "note": "No expected repo list configured; set --expected-repos-file or GOVERNANCE_ENROLLMENT_EXPECTED_REPO_LIST.",
            "aggregate_report_kind": aggregate.get("report_kind"),
            "aggregate_aggregated_at": aggregate.get("aggregated_at"),
        }
    else:
        rep = build_report(
            expected=expected,
            aggregate=aggregate,
            manifest=manifest,
            manifest_errors=merr,
        )

    rep["ok"] = True
    out_json = Path(args.json_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(render_markdown(rep), encoding="utf-8")

    if not rep.get("compliant", True):
        print(
            "::warning::Governance enrollment coverage: missing repositories or manifest errors — see JSON out",
            file=sys.stderr,
        )
        if args.fail_on_missing and expected:
            return 2
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
