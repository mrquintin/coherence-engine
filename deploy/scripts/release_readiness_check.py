#!/usr/bin/env python3
"""Release readiness checklist (prompt 20 of 20).

Runs a deterministic, network-free set of post-condition checks against
the current repository tree. Every check is self-contained and produces
a stable ``(check_id, status, reason_code, detail)`` row. Rows are
serialized into a JSON report (``--json-out``) and a human-readable
Markdown summary (``--markdown-out``).

Exit codes
----------

* ``0``  - every check passed.
* ``1``  - at least one **soft** failure was recorded. The report
  records each failing check's ``reason_code`` so CI / reviewers can
  see exactly what regressed.
* ``2``  - fixture / loader error: the script could not complete a
  check because a dependency (alembic, module import, fixture file)
  blew up. Treat this as "script not usable" rather than a product
  failure.

The script is intentionally small, single-file, stdlib + PyYAML-free,
and dependency-free beyond what the production tree already imports
(``alembic`` for the head check, ``coherence_engine`` for the
policy / event / prompt checks). It **never** makes network calls,
never writes to governed datasets, and never touches the production
database.

See ``docs/ops/release_readiness.md`` for the operator runbook and
per-check failure-mode guidance.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Repository layout: this file lives at
# ``<repo>/deploy/scripts/release_readiness_check.py``. ``<repo>`` is the
# package-root directory (``Coherence_Engine_Project/coherence_engine``);
# its parent must be on ``sys.path`` so that ``import coherence_engine``
# resolves when the script is invoked directly (mirrors the pattern in
# ``deploy/scripts/merge_governed_historical_outcomes.py``).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_PARENT = _REPO_ROOT.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))


# --- Result types -----------------------------------------------------------

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"


@dataclass
class CheckResult:
    """One row in the readiness report."""

    check_id: str
    status: str  # pass | fail | error
    reason_code: Optional[str] = None
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "evidence": self.evidence,
        }


# --- Individual checks -------------------------------------------------------

def _check_alembic_head() -> CheckResult:
    """Verify ``alembic heads`` returns exactly one head matching the expected file."""

    check_id = "alembic_head"
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except Exception as exc:  # pragma: no cover - dependency missing
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="alembic_import_failed",
            detail=f"could not import alembic: {exc}",
        )

    ini_path = _REPO_ROOT / "alembic.ini"
    if not ini_path.is_file():
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="alembic_ini_missing",
            detail=f"missing alembic.ini at {ini_path}",
        )

    try:
        cfg = Config(str(ini_path))
        sd = ScriptDirectory.from_config(cfg)
        heads = list(sd.get_heads())
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="alembic_script_load_failed",
            detail=f"alembic ScriptDirectory failed: {exc}",
        )

    evidence = {"heads": heads}

    if len(heads) != 1:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="multiple_alembic_heads",
            detail=f"expected exactly 1 head, got {len(heads)}: {heads}",
            evidence=evidence,
        )

    # Cross-check: the versions directory must contain a file whose
    # revision identifier equals the head reported by alembic.
    versions_dir = _REPO_ROOT / "alembic" / "versions"
    seen_revisions = sorted(p.name for p in versions_dir.glob("*.py") if p.is_file())
    evidence["versions_files"] = seen_revisions

    head = heads[0]
    # Each migration filename is like ``20260417_000006_workflow_checkpoints.py``;
    # the revision id is the leading ``<date>_<ordinal>`` fragment. A loose substring
    # match is deliberately sufficient here - we only care that a file with this
    # revision is present on disk.
    if not any(head in name for name in seen_revisions):
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="alembic_head_file_missing",
            detail=f"head revision {head!r} not found in {versions_dir}",
            evidence=evidence,
        )

    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail=f"single alembic head resolved to {head}",
        evidence=evidence,
    )


def _check_decision_policy_version() -> CheckResult:
    """Verify the pinned ``DECISION_POLICY_VERSION`` constant."""

    check_id = "decision_policy_version"
    try:
        from coherence_engine.server.fund.services.decision_policy import (
            DECISION_POLICY_VERSION,
        )
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="decision_policy_import_failed",
            detail=f"could not import DECISION_POLICY_VERSION: {exc}",
        )

    expected = "decision-policy-v1"
    if DECISION_POLICY_VERSION != expected:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="decision_policy_version_mismatch",
            detail=(
                f"DECISION_POLICY_VERSION={DECISION_POLICY_VERSION!r} but "
                f"expected {expected!r}"
            ),
            evidence={"observed": DECISION_POLICY_VERSION, "expected": expected},
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail=f"DECISION_POLICY_VERSION == {expected!r}",
        evidence={"observed": DECISION_POLICY_VERSION},
    )


def _check_event_schemas() -> CheckResult:
    """Load each supported event schema and validate its embedded example."""

    check_id = "event_schemas"
    try:
        from coherence_engine.server.fund.services.event_schemas import (
            SUPPORTED_EVENTS,
            load_schema,
            validate_event,
            EventValidationError,
        )
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="event_schemas_import_failed",
            detail=f"could not import event_schemas: {exc}",
        )

    checked: List[str] = []
    failures: List[str] = []
    for event_name, versions in sorted(SUPPORTED_EVENTS.items()):
        for version in versions:
            try:
                schema = load_schema(event_name, version)
            except Exception as exc:
                return CheckResult(
                    check_id=check_id,
                    status=STATUS_ERROR,
                    reason_code="event_schema_load_failed",
                    detail=f"load_schema({event_name!r}, {version!r}): {exc}",
                    evidence={"checked": checked},
                )
            examples = schema.get("examples") or []
            if not isinstance(examples, list) or not examples:
                failures.append(f"{event_name} v{version}: no examples[]")
                continue
            try:
                validate_event(event_name, examples[0], version)
            except EventValidationError as exc:
                failures.append(
                    f"{event_name} v{version} example rejected: {exc}"
                )
                continue
            except Exception as exc:
                return CheckResult(
                    check_id=check_id,
                    status=STATUS_ERROR,
                    reason_code="event_schema_validator_error",
                    detail=f"validate_event({event_name!r}): {exc}",
                    evidence={"checked": checked},
                )
            checked.append(f"{event_name}:{version}")

    evidence = {"checked": checked, "failures": failures}
    if failures:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="event_schema_example_invalid",
            detail="; ".join(failures),
            evidence=evidence,
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail=f"validated {len(checked)} event example(s)",
        evidence=evidence,
    )


def _check_prompt_registry() -> CheckResult:
    """Ensure the shipped prompt registry verifies cleanly."""

    check_id = "prompt_registry"
    try:
        from coherence_engine.server.fund.services.prompt_registry import (
            load_registry,
            verify_registry,
        )
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="prompt_registry_import_failed",
            detail=f"could not import prompt_registry: {exc}",
        )

    try:
        registry = load_registry()
        report = verify_registry(registry)
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="prompt_registry_load_failed",
            detail=f"prompt registry load/verify raised: {exc}",
        )

    report_dict = report.to_dict() if hasattr(report, "to_dict") else {
        "ok": getattr(report, "ok", False),
    }
    if not getattr(report, "ok", False):
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="prompt_registry_verify_failed",
            detail=(
                f"verify_registry returned ok=False "
                f"mismatches={len(report_dict.get('mismatches', []))} "
                f"missing={len(report_dict.get('missing', []))}"
            ),
            evidence=report_dict,
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail="prompt registry verified ok",
        evidence={"prompts": len(registry.prompts)},
    )


def _check_e2e_test_present_and_marked() -> CheckResult:
    """Confirm the end-to-end integration test file is present + ``@pytest.mark.e2e``."""

    check_id = "e2e_integration_test"
    path = _REPO_ROOT / "tests" / "integration" / "test_e2e_pipeline.py"
    if not path.is_file():
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="e2e_test_missing",
            detail=f"missing {path}",
            evidence={"path": str(path)},
        )
    try:
        body = path.read_text(encoding="utf-8")
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="e2e_test_unreadable",
            detail=f"could not read {path}: {exc}",
        )
    if not re.search(r"@pytest\.mark\.e2e\b", body):
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="e2e_marker_missing",
            detail=f"@pytest.mark.e2e not present in {path}",
            evidence={"path": str(path)},
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail="e2e integration test present with @pytest.mark.e2e",
        evidence={"path": str(path)},
    )


def _check_backtest_spec_present() -> CheckResult:
    """Backtest spec file must exist (prompt 11)."""

    check_id = "backtest_spec"
    path = _REPO_ROOT / "docs" / "specs" / "backtest_spec.md"
    if not path.is_file():
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="backtest_spec_missing",
            detail=f"missing {path}",
            evidence={"path": str(path)},
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail=f"backtest spec present at {path.relative_to(_REPO_ROOT)}",
        evidence={"path": str(path)},
    )


def _check_red_team_expected_matrix_present() -> CheckResult:
    """Red-team harness expected-verdict matrix must exist (prompt 13)."""

    check_id = "red_team_expected_matrix"
    path = _REPO_ROOT / "tests" / "adversarial" / "labels.json"
    if not path.is_file():
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="red_team_matrix_missing",
            detail=f"missing {path}",
            evidence={"path": str(path)},
        )
    # Light schema sanity: JSON parses and every entry carries an
    # ``expected_verdict`` key. This mirrors the hygiene assertion in
    # ``tests/test_red_team_harness.py`` without running the suite.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="red_team_matrix_unreadable",
            detail=f"could not parse {path}: {exc}",
        )
    if not isinstance(data, dict) or not data:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="red_team_matrix_empty",
            detail=f"{path} is not a populated JSON object",
            evidence={"path": str(path)},
        )
    missing = [k for k, v in data.items() if not isinstance(v, dict) or "expected_verdict" not in v]
    if missing:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="red_team_matrix_malformed",
            detail=f"entries missing expected_verdict: {sorted(missing)[:5]}",
            evidence={"missing": sorted(missing)},
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail=f"red-team expected matrix present ({len(data)} cases)",
        evidence={"path": str(path), "cases": len(data)},
    )


def _check_admin_dashboard_router_registered() -> CheckResult:
    """Admin dashboard router must be importable, prefixed, and mounted."""

    check_id = "admin_dashboard_router"
    try:
        from coherence_engine.server.fund.routers.admin_ui import router as admin_router
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="admin_router_import_failed",
            detail=f"could not import admin_ui router: {exc}",
        )
    prefix = getattr(admin_router, "prefix", "")
    if prefix != "/admin":
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="admin_router_prefix_wrong",
            detail=f"admin_ui router prefix={prefix!r}, expected '/admin'",
            evidence={"prefix": prefix},
        )

    # Also verify the router is referenced by create_app() source so that
    # mount drift (router exists but never wired) is caught.
    app_py = _REPO_ROOT / "server" / "fund" / "app.py"
    if not app_py.is_file():
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="app_py_missing",
            detail=f"missing {app_py}",
        )
    try:
        source = app_py.read_text(encoding="utf-8")
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="app_py_unreadable",
            detail=f"could not read {app_py}: {exc}",
        )
    if "admin_ui_router" not in source or "include_router(admin_ui_router)" not in source:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="admin_router_not_mounted",
            detail="server/fund/app.py does not call include_router(admin_ui_router)",
            evidence={"app_py": str(app_py)},
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail="admin_ui router prefix='/admin' and mounted in create_app()",
        evidence={"prefix": prefix},
    )


# Prompt-number recap checks ---------------------------------------------------

_PROMPT_NUMBERS: Tuple[str, ...] = tuple(f"{i:02d}" for i in range(1, 21))


def _prompt_numbers_referenced(body: str) -> Tuple[List[str], List[str]]:
    """Return (found, missing) prompt numbers mentioned in ``body``.

    A prompt is considered referenced if the text contains a substring like
    ``"prompt 05"`` or ``"prompt 05/20"`` (case-insensitive). The number may be
    either zero-padded or unpadded.
    """

    pattern = re.compile(r"prompt\s+0?(\d{1,2})\b", re.IGNORECASE | re.MULTILINE)
    found_numbers = set()
    for match in pattern.finditer(body):
        n = int(match.group(1))
        if 1 <= n <= 20:
            found_numbers.add(f"{n:02d}")
    found = [p for p in _PROMPT_NUMBERS if p in found_numbers]
    missing = [p for p in _PROMPT_NUMBERS if p not in found_numbers]
    return found, missing


def _check_docs_prompt_recap(filename: str, check_id: str) -> CheckResult:
    path = _REPO_ROOT / filename
    if not path.is_file():
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="docs_file_missing",
            detail=f"missing {path}",
            evidence={"path": str(path)},
        )
    try:
        body = path.read_text(encoding="utf-8")
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="docs_file_unreadable",
            detail=f"could not read {path}: {exc}",
        )
    found, missing = _prompt_numbers_referenced(body)
    evidence = {"path": str(path), "found": found, "missing": missing}
    if missing:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="docs_prompt_recap_incomplete",
            detail=(
                f"{filename} is missing recap lines for prompts: "
                f"{', '.join(missing)}"
            ),
            evidence=evidence,
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_PASS,
        detail=f"{filename} references prompts 01-20",
        evidence=evidence,
    )


def _check_migration_ci_gate() -> CheckResult:
    """Run the migration CI gate (prompt 24) and surface its verdict.

    The gate is skip-on-unconfigured by design: when ``MIGRATION_GATE_PG_URL``
    is absent it exits ``0`` with a "not configured" message, which we record
    here as a ``pass`` with reason ``migration_ci_gate_skipped`` so operators
    can see at a glance that the readiness report ran without a Postgres
    backend.
    """

    check_id = "migration_ci_gate"
    try:
        from deploy.scripts import migration_ci_gate  # type: ignore[import-not-found]
    except Exception:
        # Fall back to absolute path import — release readiness can run from a
        # checkout where ``deploy`` is not on sys.path as a package.
        import importlib.util

        gate_path = _REPO_ROOT / "deploy" / "scripts" / "migration_ci_gate.py"
        if not gate_path.is_file():
            return CheckResult(
                check_id=check_id,
                status=STATUS_ERROR,
                reason_code="migration_ci_gate_missing",
                detail=f"missing {gate_path}",
            )
        spec = importlib.util.spec_from_file_location("migration_ci_gate", gate_path)
        if spec is None or spec.loader is None:
            return CheckResult(
                check_id=check_id,
                status=STATUS_ERROR,
                reason_code="migration_ci_gate_import_failed",
                detail="could not build module spec for migration_ci_gate",
            )
        migration_ci_gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration_ci_gate)

    pg_url = os.environ.get("MIGRATION_GATE_PG_URL", "").strip()
    if not pg_url:
        return CheckResult(
            check_id=check_id,
            status=STATUS_PASS,
            reason_code="migration_ci_gate_skipped",
            detail="MIGRATION_GATE_PG_URL unset — gate skipped (CI runs the live cycle).",
            evidence={"configured": False},
        )

    try:
        exit_code, message = migration_ci_gate.run_gate(pg_url)
    except Exception as exc:
        return CheckResult(
            check_id=check_id,
            status=STATUS_ERROR,
            reason_code="migration_ci_gate_raised",
            detail=f"{type(exc).__name__}: {exc}",
        )
    if exit_code == migration_ci_gate.EXIT_OK:
        return CheckResult(
            check_id=check_id,
            status=STATUS_PASS,
            detail=message,
            evidence={"configured": True},
        )
    if exit_code == migration_ci_gate.EXIT_SCHEMA_DRIFT:
        return CheckResult(
            check_id=check_id,
            status=STATUS_FAIL,
            reason_code="migration_ci_gate_schema_drift",
            detail=message,
            evidence={"configured": True, "exit_code": exit_code},
        )
    return CheckResult(
        check_id=check_id,
        status=STATUS_ERROR,
        reason_code="migration_ci_gate_transient",
        detail=message,
        evidence={"configured": True, "exit_code": exit_code},
    )


def _check_status_doc_prompt_recap() -> CheckResult:
    return _check_docs_prompt_recap(
        "COHERENCE_ENGINE_PROJECT_STATUS.txt",
        "status_doc_prompt_recap",
    )


def _check_continuation_doc_prompt_recap() -> CheckResult:
    return _check_docs_prompt_recap(
        "COHERENCE_ENGINE_CONTINUATION_PROMPT.txt",
        "continuation_doc_prompt_recap",
    )


# Registry of checks. Ordering is stable so the JSON / Markdown report is
# also stable across runs (crucial for reproducibility).
CHECKS: Tuple[Tuple[str, Any], ...] = (
    ("alembic_head", _check_alembic_head),
    ("decision_policy_version", _check_decision_policy_version),
    ("event_schemas", _check_event_schemas),
    ("prompt_registry", _check_prompt_registry),
    ("e2e_integration_test", _check_e2e_test_present_and_marked),
    ("backtest_spec", _check_backtest_spec_present),
    ("red_team_expected_matrix", _check_red_team_expected_matrix_present),
    ("admin_dashboard_router", _check_admin_dashboard_router_registered),
    ("migration_ci_gate", _check_migration_ci_gate),
    ("status_doc_prompt_recap", _check_status_doc_prompt_recap),
    ("continuation_doc_prompt_recap", _check_continuation_doc_prompt_recap),
)


# --- Aggregation / rendering -------------------------------------------------

def run_checks() -> List[CheckResult]:
    """Execute every registered check in declaration order."""

    results: List[CheckResult] = []
    for check_id, func in CHECKS:
        try:
            result = func()
        except Exception as exc:  # pragma: no cover - defensive
            results.append(
                CheckResult(
                    check_id=check_id,
                    status=STATUS_ERROR,
                    reason_code="unhandled_exception",
                    detail=(
                        f"{type(exc).__name__}: {exc}\n"
                        + "".join(traceback.format_exc())
                    ),
                )
            )
            continue
        if result.check_id != check_id:
            # Self-consistency guard: every check function must use its own id.
            result = dataclasses.replace(result, check_id=check_id)
        results.append(result)
    return results


def build_report(results: List[CheckResult]) -> Dict[str, Any]:
    """Assemble the canonical JSON report body."""

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r.status == STATUS_PASS),
        "failed": sum(1 for r in results if r.status == STATUS_FAIL),
        "errors": sum(1 for r in results if r.status == STATUS_ERROR),
    }
    exit_code = _compute_exit_code(results)
    return {
        "schema_version": "release-readiness-report-v1",
        "exit_code": exit_code,
        "summary": summary,
        "results": [r.to_dict() for r in results],
    }


def _compute_exit_code(results: List[CheckResult]) -> int:
    if any(r.status == STATUS_ERROR for r in results):
        return 2
    if any(r.status == STATUS_FAIL for r in results):
        return 1
    return 0


def render_markdown(report: Dict[str, Any]) -> str:
    """Render a deterministic, human-readable Markdown summary."""

    lines: List[str] = []
    lines.append("# Coherence Engine Release Readiness Report")
    lines.append("")
    summary = report["summary"]
    lines.append(
        f"**Exit code:** `{report['exit_code']}`  "
        f"(pass={summary['passed']}, fail={summary['failed']}, "
        f"error={summary['errors']}, total={summary['total']})"
    )
    lines.append("")
    lines.append("| Check | Status | Reason | Detail |")
    lines.append("|-------|--------|--------|--------|")
    status_symbol = {
        STATUS_PASS: "PASS",
        STATUS_FAIL: "FAIL",
        STATUS_ERROR: "ERROR",
    }
    for row in report["results"]:
        status_cell = status_symbol.get(row["status"], row["status"].upper())
        reason_cell = row.get("reason_code") or "-"
        detail_cell = (row.get("detail") or "").replace("\n", " ").replace("|", "\\|")
        # Bound the detail cell so large error dumps do not explode the
        # rendered Markdown table; full detail is always in the JSON.
        if len(detail_cell) > 240:
            detail_cell = detail_cell[:237] + "..."
        lines.append(
            f"| `{row['check_id']}` | {status_cell} | `{reason_cell}` | {detail_cell} |"
        )
    lines.append("")
    lines.append(
        "Full machine-readable rows (including per-check `evidence`) live in the JSON report."
    )
    lines.append("")
    return "\n".join(lines)


def _canonical_json(report: Dict[str, Any]) -> str:
    """Canonical pretty-printed JSON for the report."""

    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --- CLI --------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write the machine-readable JSON report here (optional).",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=None,
        help="Write the human-readable Markdown summary here (optional).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the stdout Markdown summary (JSON / Markdown files are still written).",
    )
    args = parser.parse_args(argv)

    results = run_checks()
    report = build_report(results)

    markdown = render_markdown(report)
    json_blob = _canonical_json(report)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_blob, encoding="utf-8")
    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")

    if not args.quiet:
        sys.stdout.write(markdown)
        sys.stdout.flush()

    return int(report["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
