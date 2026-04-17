"""Tests for governed historical dataset merge + calibration export helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coherence_engine.server.fund.services.governed_historical_dataset import (
    format_governed_line,
    merge_governed_historical_datasets,
)
from coherence_engine.server.fund.services.uncertainty_calibration import to_governed_jsonl_record
from coherence_engine.server.fund.services.uncertainty_profile_registry import (
    RegistryError,
    verify_manifest_checksum,
)

ROOT = Path(__file__).resolve().parent.parent
# Imports use package name ``coherence_engine`` (the repo directory); Python needs the parent on path.
REPO_PARENT = ROOT.parent
GOV_DATA = ROOT / "data" / "governed" / "uncertainty_historical_outcomes.jsonl"
GOV_MANIFEST = ROOT / "data" / "governed" / "uncertainty_historical_outcomes.manifest.json"


def test_to_governed_jsonl_record_accepts_aliases() -> None:
    raw = {
        "superiority": 0.1,
        "observed_superiority": 0.2,
        "n_propositions": 5,
        "transcript_quality": 0.9,
        "n_contradictions": 0,
        "layer_scores": {"a": 0.5, "b": 0.5},
    }
    g = to_governed_jsonl_record(raw)
    assert g is not None
    assert g["coherence_superiority"] == pytest.approx(0.1)
    assert g["outcome_superiority"] == pytest.approx(0.2)


def test_merge_pass_through_matches_committed_bytes() -> None:
    assert GOV_DATA.is_file()
    expected = GOV_DATA.read_bytes()
    result = merge_governed_historical_datasets(GOV_DATA, [], dataset_name=GOV_DATA.name)
    assert result.body == expected
    assert result.provenance["pass_through"] is True
    verify_manifest_checksum(GOV_DATA, GOV_MANIFEST)


def test_merge_pass_through_manifest_matches_committed() -> None:
    result = merge_governed_historical_datasets(GOV_DATA, [], dataset_name=GOV_DATA.name)
    assert result.manifest["checksum_sha256"] == json.loads(GOV_MANIFEST.read_text(encoding="utf-8"))[
        "checksum_sha256"
    ]


def test_merge_appends_new_row_and_verifies(tmp_path: Path) -> None:
    inc = tmp_path / "inc.jsonl"
    inc.write_text(
        json.dumps(
            {
                "coherence_superiority": 0.11,
                "outcome_superiority": 0.22,
                "n_propositions": 7,
                "transcript_quality": 0.91,
                "n_contradictions": 0,
                "layer_scores": {
                    "contradiction": 0.5,
                    "argumentation": 0.5,
                    "embedding": 0.5,
                    "compression": 0.5,
                    "structural": 0.5,
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.jsonl"
    man = tmp_path / "out.manifest.json"
    result = merge_governed_historical_datasets(GOV_DATA, [inc], dataset_name=out.name)
    out.write_bytes(result.body)
    man.write_text(json.dumps(result.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = verify_manifest_checksum(out, man)
    assert digest == result.manifest["checksum_sha256"]
    lines = [ln for ln in result.body.splitlines() if ln.strip()]
    assert len(lines) == 4
    assert result.provenance["n_output_records"] == 4


def test_merge_strict_incoming_rejects_bad_row(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"not":"a row"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        merge_governed_historical_datasets(
            GOV_DATA,
            [bad],
            strict_incoming=True,
        )


def test_merge_skips_bad_incoming_when_not_strict(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"not":"a row"}\n', encoding="utf-8")
    result = merge_governed_historical_datasets(GOV_DATA, [bad], strict_incoming=False)
    assert result.provenance["n_incoming_records_skipped_invalid"] == 1
    assert result.provenance["n_output_records"] == 3


def test_format_governed_line_round_trip_first_committed_row() -> None:
    line = GOV_DATA.read_text(encoding="utf-8").splitlines()[0]
    rec = json.loads(line)
    out = format_governed_line(rec).rstrip("\n")
    assert out == line


def test_merge_cli_smoke(tmp_path: Path) -> None:
    script = ROOT / "deploy" / "scripts" / "merge_governed_historical_outcomes.py"
    out = tmp_path / "m.jsonl"
    man = tmp_path / "m.manifest.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--dataset",
            str(GOV_DATA),
            "--output",
            str(out),
            "--manifest-out",
            str(man),
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_PARENT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    verify_manifest_checksum(out, man)


def test_uncertainty_profile_merge_historical_dataset_cli(tmp_path: Path) -> None:
    out = tmp_path / "c.jsonl"
    man = tmp_path / "c.manifest.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "coherence_engine",
            "uncertainty-profile",
            "merge-historical-dataset",
            "--dataset",
            str(GOV_DATA),
            "--output",
            str(out),
            "--manifest-out",
            str(man),
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_PARENT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    verify_manifest_checksum(out, man)


def test_merge_reordered_rows_checksum_mismatch_without_update(tmp_path: Path) -> None:
    """When incoming is non-empty, output is sorted by fingerprint — manifest must change."""
    inc = tmp_path / "emptyish.jsonl"
    inc.write_text(
        json.dumps(
            {
                "coherence_superiority": 0.11,
                "outcome_superiority": 0.22,
                "n_propositions": 7,
                "transcript_quality": 0.91,
                "n_contradictions": 0,
                "layer_scores": {
                    "contradiction": 0.5,
                    "argumentation": 0.5,
                    "embedding": 0.5,
                    "compression": 0.5,
                    "structural": 0.5,
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out2.jsonl"
    result = merge_governed_historical_datasets(GOV_DATA, [inc], dataset_name=out.name)
    out.write_bytes(result.body)
    with pytest.raises(RegistryError, match="checksum mismatch"):
        verify_manifest_checksum(out, GOV_MANIFEST)
