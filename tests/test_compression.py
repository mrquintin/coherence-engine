"""Tests for layers/compression.py — Information-theoretic compression coherence."""

import pytest
from coherence_engine.core.types import Proposition, ArgumentStructure
from coherence_engine.layers.compression import CompressionAnalyzer


@pytest.fixture
def analyzer():
    return CompressionAnalyzer()


def _make_structure(*texts):
    props = [Proposition(id=f"P{i+1}", text=t) for i, t in enumerate(texts)]
    return ArgumentStructure(propositions=props, relations=[], original_text=" ".join(texts))


class TestCompressionCoherence:
    def test_related_text_compresses_well(self, analyzer):
        structure = _make_structure(
            "Machine learning is a subset of artificial intelligence.",
            "Artificial intelligence systems learn from data and experience.",
            "Deep learning uses neural networks for machine intelligence.",
            "Training data quality affects machine learning model performance.",
        )
        result = analyzer.analyze(structure)
        assert result.name == "compression"
        assert result.score > 0.0

    def test_unrelated_text_compresses_poorly(self, analyzer):
        structure = _make_structure(
            "The sun rises in the east every morning.",
            "Python programming uses indentation for blocks.",
            "Elephants are the largest land mammals alive.",
            "The Pythagorean theorem relates triangle sides.",
        )
        result = analyzer.analyze(structure)
        assert 0.0 <= result.score <= 1.0

    def test_highly_redundant_text(self, analyzer):
        structure = _make_structure(
            "Machine learning is important for data science.",
            "Machine learning is important for data science applications.",
            "Machine learning is very important for modern data science.",
        )
        result = analyzer.analyze(structure)
        assert result.score > 0.0
        assert result.details.get("redundancy") is not None


class TestDetails:
    def test_details_present(self, analyzer):
        structure = _make_structure(
            "First proposition is about technology.",
            "Second proposition discusses innovation.",
        )
        result = analyzer.analyze(structure)
        assert "compression_coherence" in result.details
        assert "joint_size" in result.details
        assert "sum_individual_sizes" in result.details
        assert "compression_ratio" in result.details

    def test_compression_ratio_valid(self, analyzer):
        structure = _make_structure(
            "This is the first statement about markets.",
            "This is the second statement about economics.",
        )
        result = analyzer.analyze(structure)
        ratio = result.details["compression_ratio"]
        assert 0.0 < ratio <= 2.0


class TestEdgeCases:
    def test_single_proposition(self, analyzer):
        structure = _make_structure("Only one proposition with enough text content.")
        result = analyzer.analyze(structure)
        assert result.score == 0.5

    def test_score_bounded(self, analyzer):
        structure = _make_structure(
            "A long and detailed proposition about a particular subject area.",
            "Another equally long and detailed proposition about the same exact area.",
            "Yet another similarly structured proposition on precisely the same topic.",
        )
        result = analyzer.analyze(structure)
        assert 0.0 <= result.score <= 1.0

    def test_weight_correct(self, analyzer):
        structure = _make_structure("First sentence.", "Second sentence is longer.")
        result = analyzer.analyze(structure)
        assert result.weight == 0.15
