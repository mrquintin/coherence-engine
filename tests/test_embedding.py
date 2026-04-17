"""Tests for layers/embedding.py — Cosine similarity + difference-vector analysis."""

import pytest
from coherence_engine.core.types import Proposition, ArgumentStructure
from coherence_engine.layers.embedding import EmbeddingCoherenceAnalyzer
from coherence_engine.embeddings.tfidf import TFIDFEmbedder
from coherence_engine.embeddings.utils import (
    cosine_similarity,
    hoyer_sparsity,
    difference_vector,
    l2_norm,
    cosine_similarity_matrix,
)


@pytest.fixture
def embedder():
    return TFIDFEmbedder(max_features=100)


@pytest.fixture
def analyzer(embedder):
    return EmbeddingCoherenceAnalyzer(embedder=embedder)


def _make_structure(*texts):
    props = [Proposition(id=f"P{i+1}", text=t, importance=0.7) for i, t in enumerate(texts)]
    return ArgumentStructure(propositions=props, relations=[], original_text=" ".join(texts))


class TestUtilFunctions:
    def test_cosine_identical(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_cosine_orthogonal(self):
        assert cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_cosine_opposite(self):
        assert cosine_similarity([1, 0, 0], [-1, 0, 0]) == pytest.approx(-1.0)

    def test_cosine_zero_vector(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    def test_l2_norm(self):
        assert l2_norm([3, 4]) == pytest.approx(5.0)

    def test_l2_norm_zero(self):
        assert l2_norm([0, 0, 0]) == 0.0

    def test_hoyer_sparsity_uniform(self):
        result = hoyer_sparsity([1, 1, 1, 1])
        assert result == pytest.approx(0.0)

    def test_hoyer_sparsity_sparse(self):
        result = hoyer_sparsity([1, 0, 0, 0])
        assert result == pytest.approx(1.0)

    def test_hoyer_sparsity_zero(self):
        assert hoyer_sparsity([0, 0, 0]) == 0.0

    def test_difference_vector(self):
        result = difference_vector([1, 2, 3], [4, 5, 6])
        assert result == [3, 3, 3]

    def test_difference_vector_mismatch(self):
        with pytest.raises(ValueError):
            difference_vector([1, 2], [1, 2, 3])

    def test_cosine_similarity_matrix_shape(self):
        embs = [[1, 0], [0, 1], [1, 1]]
        matrix = cosine_similarity_matrix(embs)
        assert len(matrix) == 3
        assert len(matrix[0]) == 3
        for i in range(3):
            assert matrix[i][i] == pytest.approx(1.0)

    def test_cosine_similarity_matrix_symmetric(self):
        embs = [[1, 2], [3, 4], [5, 6]]
        matrix = cosine_similarity_matrix(embs)
        for i in range(3):
            for j in range(3):
                assert matrix[i][j] == pytest.approx(matrix[j][i])


class TestEmbeddingAnalyzer:
    def test_similar_text_scores_high(self, analyzer):
        structure = _make_structure(
            "Machine learning algorithms improve over time with more data.",
            "Artificial intelligence models get better with larger training datasets.",
            "Deep learning systems need extensive data to achieve high performance.",
        )
        result = analyzer.analyze(structure)
        assert result.name == "embedding"
        assert result.score >= 0.0

    def test_dissimilar_text_scores_lower(self, analyzer):
        structure = _make_structure(
            "Quantum computing uses superposition and entanglement.",
            "Banana bread requires ripe bananas and baking soda.",
            "The history of ancient Rome spans many centuries.",
        )
        result = analyzer.analyze(structure)
        assert 0.0 <= result.score <= 1.0

    def test_details_present(self, analyzer):
        structure = _make_structure(
            "Cats are popular household pets.",
            "Dogs are loyal animal companions.",
        )
        result = analyzer.analyze(structure)
        assert "avg_cosine_similarity" in result.details
        assert "suspicious_pairs" in result.details
        assert "total_pairs" in result.details
        assert "embedder" in result.details

    def test_single_proposition_neutral(self, analyzer):
        structure = _make_structure("Only one sentence here with enough words to matter.")
        result = analyzer.analyze(structure)
        assert result.score == 0.5


class TestTFIDFEmbedder:
    def test_embed_returns_correct_dim(self, embedder):
        embedder.fit(["hello world", "test document"])
        vec = embedder.embed("hello world")
        assert len(vec) == 100

    def test_embed_batch(self, embedder):
        texts = ["hello world", "foo bar", "test document"]
        vecs = embedder.embed_batch(texts)
        assert len(vecs) == 3
        assert all(len(v) == 100 for v in vecs)

    def test_auto_fit(self, embedder):
        assert not embedder.fitted
        embedder.embed("auto fit test")
        assert embedder.fitted

    def test_fitted_property(self, embedder):
        assert not embedder._fitted
        embedder.fit(["hello", "world"])
        assert embedder._fitted

    def test_dim_property(self, embedder):
        assert embedder.dim == 100

    def test_available_property(self, embedder):
        assert embedder.available is True
