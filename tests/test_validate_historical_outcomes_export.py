"""Tests for historical outcomes export validation (pre-merge gate)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from coherence_engine.server.fund.services.governed_historical_dataset import (
    validate_historical_outcomes_export,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "deploy" / "ops" / "uncertainty-historical-outcomes-export.example.json"
SCRIPT = ROOT / "deploy" / "scripts" / "validate_historical_outcomes_export.py"


def test_example_export_passes_default_validation() -> None:
    assert EXAMPLE.is_file()
    rep = validate_historical_outcomes_export(EXAMPLE)
    assert rep.ok
    assert rep.valid_rows == 2
    assert rep.invalid_rows == 0


def test_strict_layer_keys_rejects_partial(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text(
        json.dumps(
            [
                {
                    "coherence_superiority": 0.0,
                    "outcome_superiority": 0.0,
                    "n_propositions": 1,
                    "transcript_quality": 1.0,
                    "n_contradictions": 0,
                    "layer_scores": {"contradiction": 0.5},
                }
            ]
        ),
        encoding="utf-8",
    )
    rep = validate_historical_outcomes_export(p, require_standard_layer_keys=True)
    assert not rep.ok
    assert rep.invalid_rows == 1
    assert "missing keys" in rep.errors[0]


def test_invalid_row_reported_tmp(tmp_path: Path) -> None:
    p = tmp_path / "bad.jsonl"
    p.write_text('{"not":"a row"}\n', encoding="utf-8")
    rep = validate_historical_outcomes_export(p)
    assert not rep.ok
    assert rep.invalid_rows == 1


def test_deploy_script_exit_2_on_invalid(tmp_path: Path) -> None:
    import os

    p = tmp_path / "bad_export.json"
    p.write_text("[{}]", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(p)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT.parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr + proc.stdout


def test_deploy_script_smoke() -> None:
    import os

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(EXAMPLE),
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT.parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_cli_validate_historical_export() -> None:
    import os

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "coherence_engine",
            "uncertainty-profile",
            "validate-historical-export",
            "--input",
            str(EXAMPLE),
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT.parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
