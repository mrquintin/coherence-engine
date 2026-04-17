"""Tests for fund scoring uncertainty calibration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coherence_engine.core.types import ArgumentStructure, CoherenceResult, LayerResult, Proposition
from coherence_engine.server.fund.services.scoring import ScoringService
from coherence_engine.server.fund.services.uncertainty import (
    UNCERTAINTY_MODEL_VERSION,
    calibrated_superiority_interval_95,
    contradiction_burden,
    layer_score_disagreement,
)


def _uniform_layers(val: float = 0.5) -> dict[str, float]:
    return {
        "contradiction": val,
        "argumentation": val,
        "embedding": val,
        "compression": val,
        "structural": val,
    }


def _width(lower: float, upper: float) -> float:
    return upper - lower


def test_layer_score_disagreement_zero_when_uniform():
    assert layer_score_disagreement(_uniform_layers(0.5)) == pytest.approx(0.0)


def test_layer_score_disagreement_positive_when_spread():
    d = layer_score_disagreement(
        {
            "contradiction": 0.0,
            "argumentation": 1.0,
            "embedding": 0.5,
            "compression": 0.5,
            "structural": 0.5,
        }
    )
    assert d > 0.3


def test_contradiction_burden_scales_with_props():
    assert contradiction_burden(3, 9) == pytest.approx(1.0)
    assert contradiction_burden(0, 9) == pytest.approx(0.0)


def test_ci95_width_within_policy_bounds():
    lo, hi, meta = calibrated_superiority_interval_95(
        superiority=0.0,
        n_propositions=2,
        transcript_quality=0.2,
        n_contradictions=99,
        layer_scores={
            "contradiction": 0.0,
            "argumentation": 1.0,
            "embedding": 0.0,
            "compression": 1.0,
            "structural": 0.5,
        },
    )
    w = _width(lo, hi)
    assert 0.05 <= w <= 0.25
    assert meta["uncertainty_model_version"] == UNCERTAINTY_MODEL_VERSION
    inputs = meta["calibration_inputs"]
    assert "half_width_95" in inputs
    assert inputs["half_width_95"] == pytest.approx(w / 2.0, rel=1e-5, abs=1e-5)


def test_ci95_symmetric_interior_point():
    lo, hi, _ = calibrated_superiority_interval_95(
        superiority=0.0,
        n_propositions=20,
        transcript_quality=1.0,
        n_contradictions=0,
        layer_scores=_uniform_layers(),
    )
    assert lo == pytest.approx(-(hi), rel=0, abs=1e-9)


def test_monotone_n_propositions_narrows_interval():
    base_kw = dict(
        superiority=0.25,
        transcript_quality=1.0,
        n_contradictions=0,
        layer_scores=_uniform_layers(),
    )
    lo_a, hi_a, _ = calibrated_superiority_interval_95(n_propositions=6, **base_kw)
    lo_b, hi_b, _ = calibrated_superiority_interval_95(n_propositions=60, **base_kw)
    assert _width(lo_b, hi_b) <= _width(lo_a, hi_a)


def test_monotone_transcript_quality_widens_when_worse():
    base_kw = dict(
        superiority=0.1,
        n_propositions=10,
        n_contradictions=0,
        layer_scores=_uniform_layers(),
    )
    lo_hi, hi_hi, _ = calibrated_superiority_interval_95(transcript_quality=1.0, **base_kw)
    lo_lo, hi_lo, _ = calibrated_superiority_interval_95(transcript_quality=0.2, **base_kw)
    assert _width(lo_lo, hi_lo) >= _width(lo_hi, hi_hi)


def test_monotone_contradictions_widen():
    base_kw = dict(
        superiority=0.0,
        n_propositions=12,
        transcript_quality=1.0,
        layer_scores=_uniform_layers(),
    )
    lo0, hi0, _ = calibrated_superiority_interval_95(n_contradictions=0, **base_kw)
    lo1, hi1, _ = calibrated_superiority_interval_95(n_contradictions=8, **base_kw)
    assert _width(lo1, hi1) >= _width(lo0, hi0)


def test_monotone_layer_disagreement_widens():
    base_kw = dict(
        superiority=0.0,
        n_propositions=15,
        transcript_quality=1.0,
        n_contradictions=0,
    )
    lo_u, hi_u, _ = calibrated_superiority_interval_95(layer_scores=_uniform_layers(0.5), **base_kw)
    lo_d, hi_d, _ = calibrated_superiority_interval_95(
        layer_scores={
            "contradiction": 0.05,
            "argumentation": 0.95,
            "embedding": 0.1,
            "compression": 0.9,
            "structural": 0.5,
        },
        **base_kw,
    )
    assert _width(lo_d, hi_d) >= _width(lo_u, hi_u)


def test_scoring_service_response_shape():
    prop = Proposition(id="p1", text="claim", prop_type="claim", importance=0.5)
    structure = ArgumentStructure(propositions=[prop], relations=[], original_text="x")
    layers = [
        LayerResult(name="contradiction", score=0.5, weight=0.2, details={"backend": "heuristic"}),
        LayerResult(name="argumentation", score=0.5, weight=0.2),
        LayerResult(name="embedding", score=0.5, weight=0.2),
        LayerResult(name="compression", score=0.5, weight=0.2),
        LayerResult(name="structural", score=0.5, weight=0.2),
    ]
    result = CoherenceResult(
        composite_score=0.62,
        layer_results=layers,
        argument_structure=structure,
        contradictions=[],
        metadata={"n_propositions": 3, "embedder": "tfidf"},
    )

    app = SimpleNamespace(
        domain_primary="market_economics",
        transcript_text="word " * 400,
    )

    with patch.object(ScoringService, "__init__", lambda self: None):
        svc = ScoringService.__new__(ScoringService)
        svc._scorer = SimpleNamespace(score=lambda _t: result)
        svc._comparator = SimpleNamespace(
            compare=lambda res, domains: {
                "comparisons": [{"domain_coherence": 0.50}],
            }
        )
        out = ScoringService.score_application(svc, app)

    assert "coherence_superiority_ci95" in out
    ci = out["coherence_superiority_ci95"]
    assert isinstance(ci["lower"], (int, float)) and isinstance(ci["upper"], (int, float))
    assert ci["lower"] <= ci["upper"]
    assert "uncertainty_calibration" in out
    uc = out["uncertainty_calibration"]
    assert uc["uncertainty_model_version"] == UNCERTAINTY_MODEL_VERSION
    assert "calibration_inputs" in uc
    for k in ("absolute_coherence", "baseline_coherence", "coherence_superiority", "layer_scores"):
        assert k in out
    assert isinstance(out["model_versions"], dict)
    assert "metadata_notes" in out and out["metadata_notes"] == []
