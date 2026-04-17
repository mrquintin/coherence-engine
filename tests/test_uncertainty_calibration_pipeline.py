"""Tests for uncertainty calibration CLI and grid search."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from coherence_engine.server.fund.services.uncertainty import (
    UNCERTAINTY_MODEL_VERSION,
    UncertaintyParams,
    calibrated_superiority_interval_95,
    resolve_uncertainty_params_from_environment,
)
from coherence_engine.server.fund.services.uncertainty_calibration import (
    calibrate_from_records,
    evaluate_profile,
    load_historical_records,
    normalize_records,
    run_calibration_pipeline,
)


def _uniform_layers(val: float = 0.5) -> dict[str, float]:
    return {
        "contradiction": val,
        "argumentation": val,
        "embedding": val,
        "compression": val,
        "structural": val,
    }


def test_normalize_records_accepts_aliases():
    raw = [
        {
            "superiority": 0.1,
            "y": 0.12,
            "n_propositions": 10,
            "transcript_quality": 1.0,
            "n_contradictions": 0,
            "layer_scores": _uniform_layers(),
        },
        {"incomplete": True},
    ]
    norm = normalize_records(raw)
    assert len(norm) == 1
    assert norm[0]["outcome"] == pytest.approx(0.12)


def test_evaluate_profile_zero_records():
    loss, m = evaluate_profile([], UncertaintyParams())
    assert loss == 0.0
    assert m["coverage"] == 1.0


def test_calibration_prefers_coverage_on_synthetic_shift():
    """Outcomes slightly offset from point estimates; wider intervals improve coverage."""
    records = []
    for i in range(8):
        sup = 0.0 + i * 0.01
        records.append(
            {
                "superiority": sup,
                "outcome": sup + 0.08,
                "n_propositions": 8,
                "transcript_quality": 1.0,
                "n_contradictions": 0,
                "layer_scores": _uniform_layers(),
            }
        )
    tight = UncertaintyParams(half_max=0.06)
    wide = UncertaintyParams(half_max=0.125, sigma0=0.055)
    _, m_tight = evaluate_profile(records, tight, target_coverage=0.95, width_penalty=0.01)
    _, m_wide = evaluate_profile(records, wide, target_coverage=0.95, width_penalty=0.01)
    assert m_wide["coverage"] >= m_tight["coverage"]


def test_calibrate_from_records_deterministic():
    records = normalize_records(
        [
            {
                "coherence_superiority": 0.0,
                "outcome_superiority": 0.0,
                "n_propositions": 20,
                "transcript_quality": 1.0,
                "n_contradictions": 0,
                "layer_scores": _uniform_layers(),
            }
        ]
    )
    grid = {
        "sigma0": (0.045,),
        "alpha_quality": (0.55,),
        "alpha_burden": (0.35,),
        "alpha_disagreement": (0.90,),
        "half_min": (0.025,),
        "half_max": (0.125,),
    }
    a = calibrate_from_records(records, grid=grid)
    b = calibrate_from_records(records, grid=grid)
    assert a == b
    assert a["best_parameters"]["sigma0"] == pytest.approx(0.045)
    assert a["uncertainty_model_version"] == UNCERTAINTY_MODEL_VERSION


def test_load_json_and_jsonl(tmp_path):
    p1 = tmp_path / "a.json"
    p1.write_text(
        json.dumps(
            [
                {
                    "coherence_superiority": 0.0,
                    "outcome_superiority": 0.0,
                    "n_propositions": 5,
                    "transcript_quality": 0.9,
                    "n_contradictions": 1,
                    "layer_scores": _uniform_layers(),
                }
            ]
        ),
        encoding="utf-8",
    )
    assert len(load_historical_records(str(p1))) == 1

    p2 = tmp_path / "b.jsonl"
    row = {
        "coherence_superiority": 0.1,
        "outcome_superiority": 0.1,
        "n_propositions": 5,
        "transcript_quality": 0.9,
        "n_contradictions": 0,
        "layer_scores": _uniform_layers(),
    }
    p2.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert len(load_historical_records(str(p2))) == 1


def test_run_calibration_pipeline_counts_skipped(tmp_path):
    p = tmp_path / "mix.json"
    p.write_text(
        json.dumps(
            [
                {
                    "coherence_superiority": 0.0,
                    "outcome_superiority": 0.0,
                    "n_propositions": 3,
                    "transcript_quality": 1.0,
                    "layer_scores": _uniform_layers(),
                },
                {"nope": 1},
            ]
        ),
        encoding="utf-8",
    )
    out = run_calibration_pipeline(str(p))
    assert out["n_records_loaded"] == 2
    assert out["n_records_used"] == 1
    assert out["n_records_skipped"] == 1


def test_env_profile_overrides_interval(monkeypatch):
    monkeypatch.delenv("COHERENCE_UNCERTAINTY_PROFILE_PATH", raising=False)
    monkeypatch.setenv(
        "COHERENCE_UNCERTAINTY_PROFILE",
        json.dumps({"sigma0": 0.08, "half_max": 0.125}),
    )
    lo_d, hi_d, _ = calibrated_superiority_interval_95(
        superiority=0.0,
        n_propositions=4,
        transcript_quality=1.0,
        n_contradictions=0,
        layer_scores=_uniform_layers(),
    )
    monkeypatch.delenv("COHERENCE_UNCERTAINTY_PROFILE", raising=False)
    lo_b, hi_b, _ = calibrated_superiority_interval_95(
        superiority=0.0,
        n_propositions=4,
        transcript_quality=1.0,
        n_contradictions=0,
        layer_scores=_uniform_layers(),
        params=UncertaintyParams(),
    )
    assert (hi_d - lo_d) > (hi_b - lo_b)


def test_resolve_uncertainty_params_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COHERENCE_UNCERTAINTY_PROFILE", raising=False)
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"alpha_quality": 0.1}), encoding="utf-8")
    monkeypatch.setenv("COHERENCE_UNCERTAINTY_PROFILE_PATH", str(prof))
    p = resolve_uncertainty_params_from_environment()
    assert p.alpha_quality == pytest.approx(0.1)
    assert p.sigma0 == pytest.approx(0.045)


def test_cli_calibrate_uncertainty_json(tmp_path):
    data = [
        {
            "coherence_superiority": 0.0,
            "outcome_superiority": 0.0,
            "n_propositions": 30,
            "transcript_quality": 1.0,
            "n_contradictions": 0,
            "layer_scores": _uniform_layers(),
        }
    ]
    inp = tmp_path / "hist.json"
    inp.write_text(json.dumps(data), encoding="utf-8")
    out_file = tmp_path / "out.json"
    # Same layout as tests/test_cli.py: package lives under repo parent.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "coherence_engine",
            "calibrate-uncertainty",
            str(inp),
            "--output",
            str(out_file),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "best_parameters" in payload
    assert payload["n_records_used"] == 1
    assert out_file.is_file()
    assert json.loads(out_file.read_text(encoding="utf-8")) == payload
