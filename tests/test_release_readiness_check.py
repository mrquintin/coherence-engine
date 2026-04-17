"""Tests for deploy/scripts/release_readiness_check.py (prompt 20).

Two classes of assertions per the prompt contract:

1. Running the readiness script against the current tree exits 0 and every
   committed check reports ``status == "pass"``.
2. Temporarily removing a required file causes the script to exit 1 with the
   specific ``reason_code`` declared for that check in the JSON report.

The script is loaded via ``importlib.util.spec_from_file_location`` so the
tests do not depend on ``deploy.scripts`` being an importable package (it
intentionally ships as a collection of loose scripts).

Forced-failure tests move a real repo file to a temp location and restore it in
a ``try`` / ``finally`` so the tree is never left in a mutated state even if
the assertion block raises.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent  # ``Coherence_Engine_Project/coherence_engine``
_SCRIPT_PATH = _REPO_ROOT / "deploy" / "scripts" / "release_readiness_check.py"


_MODULE_NAME = "_release_readiness_check_under_test"


def _load_module():
    assert _SCRIPT_PATH.is_file(), f"script missing: {_SCRIPT_PATH}"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_SCRIPT_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # NOTE: Python 3.13's ``dataclass`` decorator resolves class annotations via
    # ``sys.modules[cls.__module__].__dict__``; if we don't register the module
    # here first, every ``@dataclass`` inside the script raises
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'`` during
    # ``exec_module``. See https://docs.python.org/3/library/importlib.html#importlib.util.spec_from_file_location
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_script_exits_zero_on_clean_tree(mod, tmp_path: Path):
    json_out = tmp_path / "readiness.json"
    md_out = tmp_path / "readiness.md"
    exit_code = mod.main(
        [
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(md_out),
            "--quiet",
        ]
    )
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert exit_code == 0, (
        f"expected 0, got {exit_code}.\nReport:\n"
        + json.dumps(report, indent=2, sort_keys=True)
    )
    assert report["exit_code"] == 0
    assert report["schema_version"] == "release-readiness-report-v1"
    assert report["summary"]["errors"] == 0
    assert report["summary"]["failed"] == 0
    assert report["summary"]["passed"] == report["summary"]["total"]
    assert md_out.is_file() and md_out.read_text(encoding="utf-8").startswith("#")


def test_all_checks_report_pass_on_clean_tree(mod):
    results = mod.run_checks()
    statuses = {r.check_id: (r.status, r.reason_code, r.detail) for r in results}
    non_pass = {cid: s for cid, s in statuses.items() if s[0] != "pass"}
    assert not non_pass, f"non-pass checks: {non_pass}"


def test_report_rows_follow_registered_order(mod):
    """Result rows must match the ``CHECKS`` tuple order so the JSON is stable."""
    results = mod.run_checks()
    declared = [check_id for check_id, _ in mod.CHECKS]
    assert [r.check_id for r in results] == declared


def test_report_ids_are_unique_and_non_empty(mod):
    ids = [check_id for check_id, _ in mod.CHECKS]
    assert len(ids) == len(set(ids)), "duplicate check_id in CHECKS registry"
    assert all(ids), "empty check_id in CHECKS registry"


# ---------------------------------------------------------------------------
# Content shape
# ---------------------------------------------------------------------------

def test_markdown_summary_mentions_every_check_id(mod, tmp_path: Path):
    md_out = tmp_path / "summary.md"
    mod.main(["--markdown-out", str(md_out), "--quiet"])
    body = md_out.read_text(encoding="utf-8")
    for check_id, _ in mod.CHECKS:
        assert f"`{check_id}`" in body, f"markdown missing check_id={check_id}"


def test_json_report_is_byte_stable_across_runs(mod, tmp_path: Path):
    """The JSON report must be byte-identical on two back-to-back runs."""
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"
    mod.main(["--json-out", str(out_a), "--quiet"])
    mod.main(["--json-out", str(out_b), "--quiet"])
    assert out_a.read_text(encoding="utf-8") == out_b.read_text(encoding="utf-8")


def test_canonical_json_renders_sorted_keys(mod):
    """Internal helper must emit sorted-key JSON for determinism."""
    sample = {
        "schema_version": "release-readiness-report-v1",
        "exit_code": 0,
        "summary": {"total": 0, "passed": 0, "failed": 0, "errors": 0},
        "results": [],
    }
    rendered = mod._canonical_json(sample)
    # sort_keys=True puts ``exit_code`` before ``results`` / ``schema_version``
    idx_ec = rendered.index('"exit_code"')
    idx_rs = rendered.index('"results"')
    idx_sv = rendered.index('"schema_version"')
    assert idx_ec < idx_rs < idx_sv


def test_run_checks_returns_check_result_instances(mod):
    results = mod.run_checks()
    assert results and all(isinstance(r, mod.CheckResult) for r in results)


# ---------------------------------------------------------------------------
# Forced failures: reason_code surfaced in JSON report
# ---------------------------------------------------------------------------

def _run_and_load(mod, tmp_path: Path) -> dict:
    out = tmp_path / "forced.json"
    exit_code = mod.main(["--json-out", str(out), "--quiet"])
    return {
        "exit_code": exit_code,
        "report": json.loads(out.read_text(encoding="utf-8")),
    }


def test_missing_red_team_matrix_yields_specific_reason_code(mod, tmp_path: Path):
    labels = _REPO_ROOT / "tests" / "adversarial" / "labels.json"
    backup = tmp_path / "labels.json.bak"
    assert labels.is_file(), "precondition: red-team labels file must exist"
    try:
        shutil.move(str(labels), str(backup))
        assert not labels.is_file(), "labels.json should be gone during the check"
        observed = _run_and_load(mod, tmp_path)
        assert observed["exit_code"] == 1, observed
        row = next(
            r for r in observed["report"]["results"]
            if r["check_id"] == "red_team_expected_matrix"
        )
        assert row["status"] == "fail"
        assert row["reason_code"] == "red_team_matrix_missing"
    finally:
        if backup.is_file() and not labels.is_file():
            shutil.move(str(backup), str(labels))
    assert labels.is_file(), "postcondition: labels.json must be restored"


def test_missing_backtest_spec_yields_specific_reason_code(mod, tmp_path: Path):
    spec = _REPO_ROOT / "docs" / "specs" / "backtest_spec.md"
    backup = tmp_path / "backtest_spec.md.bak"
    assert spec.is_file(), "precondition: backtest spec must exist"
    try:
        shutil.move(str(spec), str(backup))
        assert not spec.is_file()
        observed = _run_and_load(mod, tmp_path)
        assert observed["exit_code"] == 1, observed
        row = next(
            r for r in observed["report"]["results"]
            if r["check_id"] == "backtest_spec"
        )
        assert row["status"] == "fail"
        assert row["reason_code"] == "backtest_spec_missing"
    finally:
        if backup.is_file() and not spec.is_file():
            shutil.move(str(backup), str(spec))
    assert spec.is_file(), "postcondition: backtest spec must be restored"


def test_missing_e2e_test_yields_marker_reason_code(mod, tmp_path: Path):
    path = _REPO_ROOT / "tests" / "integration" / "test_e2e_pipeline.py"
    backup = tmp_path / "test_e2e_pipeline.py.bak"
    assert path.is_file(), "precondition: e2e integration test must exist"
    try:
        shutil.move(str(path), str(backup))
        assert not path.is_file()
        observed = _run_and_load(mod, tmp_path)
        assert observed["exit_code"] == 1, observed
        row = next(
            r for r in observed["report"]["results"]
            if r["check_id"] == "e2e_integration_test"
        )
        assert row["status"] == "fail"
        assert row["reason_code"] == "e2e_test_missing"
    finally:
        if backup.is_file() and not path.is_file():
            shutil.move(str(backup), str(path))
    assert path.is_file(), "postcondition: e2e integration test must be restored"


# ---------------------------------------------------------------------------
# CLI smoke (end-to-end subprocess path used by the Makefile target)
# ---------------------------------------------------------------------------

def test_cli_subprocess_run_prints_report(tmp_path: Path):
    """Executing the script as a subprocess must emit the Markdown summary."""
    json_out = tmp_path / "sub.json"
    md_out = tmp_path / "sub.md"
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT_PATH),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(md_out),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, (
        f"subprocess failed with {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "Coherence Engine Release Readiness Report" in proc.stdout
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["exit_code"] == 0
    assert report["summary"]["failed"] == 0
