"""Tests for the deterministic anti-gaming detector (Wave 2, prompt 09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.core.anti_gaming import (
    AG_CONTRADICTION_DENIAL,
    AG_FLUENCY_WITHOUT_CONTENT,
    AG_PRIOR_CORPUS_ECHO,
    AG_REPETITIVE_FILLER,
    AG_TEMPLATE_OVERLAP,
    AntiGamingReport,
    FLAG_WEIGHTS,
    detect_anti_gaming,
)
from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.core.types import Proposition
from coherence_engine.config import EngineConfig


FIXTURES = Path(__file__).parent / "fixtures" / "anti_gaming"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fixture_propositions(data: dict):
    return [
        Proposition(id=p["id"], text=p["text"], prop_type=p.get("prop_type", "premise"))
        for p in data["propositions"]
    ]


def test_detector_returns_frozen_report_for_clean_fixture():
    data = _load_fixture("clean.json")
    report = detect_anti_gaming(
        _fixture_propositions(data),
        prior_corpus=data.get("prior_corpus", []),
        templates=data.get("templates", []),
    )
    assert isinstance(report, AntiGamingReport)
    assert report.flags == ()
    assert report.score == pytest.approx(1.0)
    assert set(report.metrics.keys()) >= {
        "template_overlap_max",
        "self_similarity_mean",
        "prior_corpus_overlap_max",
        "fluency_ratio",
        "contradiction_denial_count",
    }


def test_template_echo_fixture_flags_template_overlap():
    data = _load_fixture("template_echo.json")
    report = detect_anti_gaming(
        _fixture_propositions(data),
        templates=data.get("templates", []),
    )
    assert AG_TEMPLATE_OVERLAP in report.flags
    assert report.metrics["template_overlap_max"] > 0.6
    assert report.score < 1.0


def test_repetitive_fixture_flags_repetitive_filler():
    data = _load_fixture("repetitive.json")
    report = detect_anti_gaming(_fixture_propositions(data))
    assert AG_REPETITIVE_FILLER in report.flags
    assert report.metrics["self_similarity_mean"] > 0.85
    assert report.score < 1.0


def test_clean_score_is_exactly_one_when_no_flags():
    report = detect_anti_gaming(
        [
            Proposition(id="p1", text="Revenue grew 42 percent last quarter."),
            Proposition(id="p2", text="Churn dropped from 6.2 percent to 3.1 percent."),
            Proposition(id="p3", text="Our cohort of 400 pilots signed extension contracts."),
        ]
    )
    assert report.flags == ()
    assert report.score == 1.0


def test_empty_propositions_return_clean_report():
    report = detect_anti_gaming([])
    assert report.score == 1.0
    assert report.flags == ()
    assert report.metrics["template_overlap_max"] == 0.0


def test_prior_corpus_echo_is_flagged():
    prior = [
        "We close large enterprise deals through land-and-expand motions driven by product-led growth.",
    ]
    props = [
        Proposition(
            id="p1",
            text="We close large enterprise deals through land-and-expand motions driven by product-led growth.",
        ),
        Proposition(id="p2", text="Our net revenue retention exceeds 140 percent."),
    ]
    report = detect_anti_gaming(props, prior_corpus=prior)
    assert AG_PRIOR_CORPUS_ECHO in report.flags


def test_detector_is_deterministic_across_repeated_calls():
    data = _load_fixture("template_echo.json")
    props = _fixture_propositions(data)
    templates = data.get("templates", [])
    r1 = detect_anti_gaming(props, templates=templates)
    r2 = detect_anti_gaming(props, templates=templates)
    assert r1.score == r2.score
    assert r1.flags == r2.flags
    assert r1.metrics == r2.metrics


def test_bounded_penalty_never_drops_below_half_of_raw_composite():
    """Property: composite = raw * (0.5 + 0.5*ag). With ag in [0,1], ratio >= 0.5."""
    scorer = CoherenceScorer(
        EngineConfig(embedder="tfidf", contradiction_backend="heuristic")
    )
    text = (
        "Our platform onboarded 500 customers last quarter. "
        "Retention improved from 78 percent to 91 percent. "
        "We have zero product-led churn among enterprise cohorts. "
        "Therefore we can scale to 5,000 customers this fiscal year."
    )
    clean = scorer.score(text, anti_gaming=True)
    raw = float(clean.metadata.get("raw_composite_score", clean.composite_score))
    assert raw > 0.0
    assert clean.composite_score >= 0.5 * raw - 1e-9
    assert clean.composite_score <= raw + 1e-9


def test_clean_pitch_preserves_composite_via_multiplier_of_one():
    scorer = CoherenceScorer(
        EngineConfig(embedder="tfidf", contradiction_backend="heuristic")
    )
    text = (
        "We serve 900 paying customers in logistics. "
        "Gross margin improved from 62 percent to 74 percent. "
        "Three enterprise pilots converted to annual contracts. "
        "Therefore we can reach $2M ARR this year."
    )
    result = scorer.score(text, anti_gaming=True)
    raw = float(result.metadata["raw_composite_score"])
    assert result.metadata["anti_gaming_score"] == pytest.approx(1.0)
    assert result.composite_score == pytest.approx(raw)


def test_anti_gaming_flag_disabled_skips_penalty_and_records_clean_default():
    scorer = CoherenceScorer(
        EngineConfig(embedder="tfidf", contradiction_backend="heuristic")
    )
    text = (
        "Scalable platform leverages scalable technology scalable scalable scalable. "
        "Scalable technology platform leverages scalable platform scalable. "
        "Scalable platform scalable scalable scalable scalable platform. "
        "Scalable scalable scalable scalable scalable scalable scalable."
    )
    with_ag = scorer.score(text, anti_gaming=True)
    without_ag = scorer.score(text, anti_gaming=False)
    assert without_ag.metadata["anti_gaming_score"] == pytest.approx(1.0)
    assert without_ag.metadata["anti_gaming_flags"] == []
    assert with_ag.composite_score <= without_ag.composite_score + 1e-9


def test_flag_weights_are_all_in_unit_interval():
    for name, w in FLAG_WEIGHTS.items():
        assert 0.0 < w <= 1.0, (name, w)
    assert set(FLAG_WEIGHTS.keys()) == {
        AG_TEMPLATE_OVERLAP,
        AG_REPETITIVE_FILLER,
        AG_PRIOR_CORPUS_ECHO,
        AG_FLUENCY_WITHOUT_CONTENT,
        AG_CONTRADICTION_DENIAL,
    }


def test_scoring_service_passes_through_anti_gaming_fields():
    """ScoringService exposes anti_gaming_score/flags/metrics in its return dict."""
    from coherence_engine.server.fund.services.scoring import ScoringService

    class _App:
        id = "app_test"
        domain_primary = "market_economics"
        one_liner = "Autonomous quality control for electronics manufacturers."
        use_of_funds_summary = "Expand sales and engineering."
        transcript_text = (
            "We onboarded 480 factories last year. "
            "Defect escape dropped from 2.1 percent to 0.7 percent. "
            "ARR grew from $220k to $1.1M. "
            "We are raising to scale field engineering."
        )

    out = ScoringService().score_application(_App())
    assert "anti_gaming_score" in out
    assert 0.0 <= out["anti_gaming_score"] <= 1.0
    assert "anti_gaming_flags" in out
    assert isinstance(out["anti_gaming_flags"], list)
    assert "anti_gaming_metrics" in out
