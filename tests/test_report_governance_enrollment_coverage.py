"""Tests for deploy/scripts/report_governance_enrollment_coverage.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "scripts" / "report_governance_enrollment_coverage.py"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        env={**os.environ, **(env or {}), "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def sample_aggregate(tmp_path: Path) -> Path:
    p = tmp_path / "governance-attestation-trend-aggregate.json"
    doc = {
        "report_kind": "governance_attestation_trend_aggregate",
        "aggregated_at": "2026-04-01T00:00:00Z",
        "latest_snapshot_by_repository": {
            "acme/a": {"x": 1},
            "acme/b": {"x": 2},
        },
        "inputs": [],
    }
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_enrollment_compliant_when_expected_matches(sample_aggregate: Path, tmp_path: Path) -> None:
    exp = tmp_path / "expected.txt"
    exp.write_text("acme/a\nacme/b\n", encoding="utf-8")
    out = tmp_path / "out.json"
    proc = _run(
        "--aggregate-json",
        str(sample_aggregate),
        "--expected-repos-file",
        str(exp),
        "--json-out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["compliant"] is True
    assert rep["missing_repositories"] == []


def test_enrollment_missing_repo_exit_0_without_fail_flag(
    sample_aggregate: Path, tmp_path: Path
) -> None:
    exp = tmp_path / "expected.txt"
    exp.write_text("acme/a\nacme/missing\n", encoding="utf-8")
    out = tmp_path / "out.json"
    proc = _run(
        "--aggregate-json",
        str(sample_aggregate),
        "--expected-repos-file",
        str(exp),
        "--json-out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["compliant"] is False
    assert "acme/missing" in rep["missing_repositories"]


def test_enrollment_fail_on_missing_returns_2(sample_aggregate: Path, tmp_path: Path) -> None:
    exp = tmp_path / "expected.txt"
    exp.write_text("acme/z\n", encoding="utf-8")
    out = tmp_path / "out.json"
    proc = _run(
        "--aggregate-json",
        str(sample_aggregate),
        "--expected-repos-file",
        str(exp),
        "--json-out",
        str(out),
        "--fail-on-missing",
    )
    assert proc.returncode == 2
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["compliant"] is False


def test_exception_manifest_allows_missing(sample_aggregate: Path, tmp_path: Path) -> None:
    exp = tmp_path / "expected.txt"
    exp.write_text("acme/a\nacme/b\nacme/excused\n", encoding="utf-8")
    man = tmp_path / "manifest.json"
    man.write_text(
        json.dumps({"allowed_missing_repositories": ["acme/excused"]}),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    proc = _run(
        "--aggregate-json",
        str(sample_aggregate),
        "--expected-repos-file",
        str(exp),
        "--exception-manifest",
        str(man),
        "--json-out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["compliant"] is True
    assert "acme/excused" in rep["allowed_missing_repositories_effective"]
