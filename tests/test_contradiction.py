"""Tests for layers/contradiction.py — Heuristic + NLI contradiction detection."""

import pytest
from coherence_engine.core.types import Proposition, ArgumentStructure
from coherence_engine.layers.contradiction import (
    HeuristicContradictionDetector,
    ContradictionDetector,
)


@pytest.fixture
def heuristic():
    return HeuristicContradictionDetector()


@pytest.fixture
def detector():
    return ContradictionDetector(backend="heuristic")


def _make_props(*texts):
    return [
        Proposition(id=f"P{i+1}", text=t, importance=0.7)
        for i, t in enumerate(texts)
    ]


def _make_structure(*texts):
    props = _make_props(*texts)
    return ArgumentStructure(propositions=props, relations=[], original_text=" ".join(texts))


class TestHeuristicAntonyms:
    def test_antonym_pair_detected(self, heuristic):
        props = _make_props(
            "The economy will increase significantly next year.",
            "The economy will decrease significantly next year.",
        )
        score, contradictions = heuristic.detect(props)
        assert len(contradictions) >= 1
        assert score < 1.0

    def test_no_false_positive_unrelated(self, heuristic):
        props = _make_props(
            "Apples are a healthy fruit choice.",
            "The weather forecast predicts rain tomorrow.",
        )
        score, contradictions = heuristic.detect(props)
        assert len(contradictions) == 0
        assert score == 1.0


class TestHeuristicNegation:
    def test_negation_detected(self, heuristic):
        props = _make_props(
            "We will invest in renewable energy projects.",
            "We will not invest in renewable energy projects.",
        )
        score, contradictions = heuristic.detect(props)
        assert len(contradictions) >= 1

    def test_negation_requires_shared_content(self, heuristic):
        props = _make_props(
            "The company is not profitable.",
            "We should celebrate this outcome.",
        )
        score, contradictions = heuristic.detect(props)
        assert len(contradictions) == 0


class TestHeuristicCommitment:
    def test_commitment_conflict(self, heuristic):
        props = _make_props(
            "We are committed to investing in clean energy solutions.",
            "We will never invest in clean energy solutions.",
        )
        score, contradictions = heuristic.detect(props)
        assert len(contradictions) >= 1


class TestContradictionDetectorFacade:
    def test_analyze_returns_layer_result(self, detector):
        structure = _make_structure(
            "Taxes should be lowered to stimulate growth.",
            "Taxes should be increased to fund public services.",
        )
        result = detector.analyze(structure)
        assert result.name == "contradiction"
        assert 0.0 <= result.score <= 1.0
        assert result.weight == 0.30

    def test_consistent_text_scores_high(self, detector):
        structure = _make_structure(
            "Exercise improves cardiovascular health over time.",
            "Regular physical activity strengthens the heart muscle.",
            "A healthy heart leads to better overall fitness.",
        )
        result = detector.analyze(structure)
        assert result.score >= 0.5

    def test_contradictory_text_scores_low(self, detector):
        structure = _make_structure(
            "We are committed to environmental sustainability.",
            "We will increase our carbon emissions by fifty percent.",
            "We will never invest in renewable energy.",
            "Protecting the environment is our top priority.",
        )
        result = detector.analyze(structure)
        assert result.score < 0.9

    def test_backend_reported_in_details(self, detector):
        structure = _make_structure("Simple test sentence one.", "Simple test sentence two.")
        result = detector.analyze(structure)
        assert "backend" in result.details
        assert result.details["backend"] in ("heuristic", "nli")


class TestEdgeCases:
    def test_single_proposition(self, detector):
        structure = _make_structure("Only one proposition here with enough words.")
        result = detector.analyze(structure)
        assert result.score >= 0.0

    def test_identical_propositions(self, heuristic):
        props = _make_props(
            "The sky is blue and beautiful today.",
            "The sky is blue and beautiful today.",
        )
        score, contradictions = heuristic.detect(props)
        assert score == 1.0
