"""Integration tests for core/scorer.py — Full pipeline."""

import os
import json
import pytest
from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.core.types import CoherenceResult
from coherence_engine.config import EngineConfig


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def scorer():
    return CoherenceScorer(EngineConfig())


@pytest.fixture
def expected():
    with open(os.path.join(FIXTURES, "expected_results.json")) as f:
        return json.load(f)


class TestFullPipeline:
    def test_basic_score(self, scorer):
        result = scorer.score(
            "The economy is growing. Employment rates are rising. "
            "Therefore we conclude that fiscal policy is working."
        )
        assert isinstance(result, CoherenceResult)
        assert 0.0 <= result.composite_score <= 1.0

    def test_all_layers_present(self, scorer):
        result = scorer.score(
            "Point one is important. Point two follows. Thus the conclusion holds."
        )
        layer_names = {r.name for r in result.layer_results}
        assert "contradiction" in layer_names
        assert "argumentation" in layer_names
        assert "embedding" in layer_names
        assert "compression" in layer_names
        assert "structural" in layer_names

    def test_weights_sum_to_one(self, scorer):
        result = scorer.score("First claim. Second premise. Therefore conclusion.")
        total_weight = sum(r.weight for r in result.layer_results)
        assert abs(total_weight - 1.0) < 0.01

    def test_composite_matches_weighted_sum(self, scorer):
        result = scorer.score(
            "Innovation drives progress. Technology improves daily lives. "
            "Therefore we conclude that investment in research is vital."
        )
        expected_composite = sum(r.score * r.weight for r in result.layer_results)
        assert abs(result.composite_score - expected_composite) < 0.01


class TestCoherentText:
    def test_coherent_essay(self, scorer, expected):
        with open(os.path.join(FIXTURES, "coherent_essay.txt")) as f:
            text = f.read()
        result = scorer.score(text)
        exp = expected["coherent_essay"]
        assert result.composite_score >= exp["min_composite"]
        assert result.composite_score <= exp["max_composite"]
        assert result.argument_structure.n_propositions >= exp["min_propositions"]


class TestContradictoryText:
    def test_contradictory_pitch(self, scorer, expected):
        with open(os.path.join(FIXTURES, "contradictory_pitch.txt")) as f:
            text = f.read()
        result = scorer.score(text)
        exp = expected["contradictory_pitch"]
        assert result.composite_score <= exp["max_composite"]
        assert result.argument_structure.n_propositions >= exp["min_propositions"]
        assert len(result.contradictions) >= exp["expected_min_contradictions"]


class TestEdgeCases:
    def test_empty_input(self, scorer):
        result = scorer.score("")
        assert result.composite_score == 0.0

    def test_single_sentence(self, scorer):
        result = scorer.score("This is a single sentence with enough words to parse properly.")
        assert result.composite_score == 0.0

    def test_two_sentences(self, scorer):
        result = scorer.score("Point one is significant. Point two follows logically.")
        assert 0.0 <= result.composite_score <= 1.0

    def test_metadata_populated(self, scorer):
        result = scorer.score("First point. Second point. Third point has more words.")
        assert "elapsed_seconds" in result.metadata
        assert "n_propositions" in result.metadata
        assert "embedder" in result.metadata


class TestCustomWeights:
    def test_custom_weights_applied(self):
        config = EngineConfig(
            weight_contradiction=0.50,
            weight_argumentation=0.10,
            weight_embedding=0.20,
            weight_compression=0.10,
            weight_structural=0.10,
        )
        scorer = CoherenceScorer(config)
        result = scorer.score("First important point. Second supporting evidence. Thus the conclusion.")
        assert result.layer_results[0].weight == 0.50

    def test_invalid_weights_raise(self):
        config = EngineConfig(
            weight_contradiction=0.50,
            weight_argumentation=0.50,
            weight_embedding=0.50,
            weight_compression=0.50,
            weight_structural=0.50,
        )
        with pytest.raises(ValueError):
            CoherenceScorer(config)


class TestReports:
    def test_text_report(self, scorer):
        result = scorer.score("The argument is sound. The evidence supports it. Therefore the conclusion holds.")
        report = result.report(fmt="text")
        assert "COHERENCE" in report
        assert "Composite Score" in report

    def test_json_report(self, scorer):
        result = scorer.score("The argument is sound. The evidence supports it. Therefore the conclusion holds.")
        report = result.report(fmt="json")
        data = json.loads(report)
        assert "composite_score" in data
        assert "layers" in data

    def test_markdown_report(self, scorer):
        result = scorer.score("The argument is sound. The evidence supports it. Therefore the conclusion holds.")
        report = result.report(fmt="markdown")
        assert "# Coherence Engine" in report
        assert "Layer" in report

    def test_to_dict(self, scorer):
        result = scorer.score("First point. Second point. Therefore the conclusion.")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "composite_score" in d
        assert "layers" in d


class TestScoreFile:
    def test_score_file(self, scorer):
        path = os.path.join(FIXTURES, "coherent_essay.txt")
        result = scorer.score_file(path)
        assert isinstance(result, CoherenceResult)
        assert result.composite_score > 0.0
