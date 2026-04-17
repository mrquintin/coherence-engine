"""Layer 3 — Embedding coherence with difference-vector analysis."""

from coherence_engine.core.types import ArgumentStructure, LayerResult

THRESHOLDS = {
    "SBERTEmbedder": {"cosine": 0.70, "sparsity": 0.30},
    "TFIDFEmbedder": {"cosine": 0.50, "sparsity": 0.25},
    "default":       {"cosine": 0.50, "sparsity": 0.25},
}


class EmbeddingCoherenceAnalyzer:
    """Analyzes coherence using embedding similarity and difference-vector analysis."""

    def __init__(self, embedder=None):
        self._embedder = embedder

    def analyze(self, structure: ArgumentStructure) -> LayerResult:
        if not self._embedder:
            from coherence_engine.embeddings.base import get_embedder
            self._embedder = get_embedder()

        embedder_name = type(self._embedder).__name__
        thresholds = THRESHOLDS.get(embedder_name, THRESHOLDS["default"])
        cosine_thresh = thresholds["cosine"]
        sparsity_thresh = thresholds["sparsity"]

        texts = [p.text for p in structure.propositions]

        if hasattr(self._embedder, 'fit'):
            self._embedder.fit(texts)

        embeddings = self._embedder.embed_batch(texts)

        from coherence_engine.embeddings.utils import (
            cosine_similarity,
            hoyer_sparsity,
            difference_vector
        )

        n = len(embeddings)

        if n < 2:
            return LayerResult(
                name="embedding",
                score=0.5,
                weight=0.20,
                details={"reason": "too few propositions"}
            )

        total_sim = 0.0
        n_pairs = 0
        suspicious_pairs = 0
        min_sim = 1.0
        max_sim = 0.0

        for i in range(n):
            for j in range(i + 1, n):
                sim = cosine_similarity(embeddings[i], embeddings[j])
                total_sim += sim
                n_pairs += 1
                min_sim = min(min_sim, sim)
                max_sim = max(max_sim, sim)

                d = difference_vector(embeddings[i], embeddings[j])
                sparsity = hoyer_sparsity(d)

                if sim > cosine_thresh and sparsity > sparsity_thresh:
                    suspicious_pairs += 1

        avg_sim = total_sim / max(n_pairs, 1)
        suspicious_ratio = suspicious_pairs / max(n_pairs, 1)

        score = avg_sim * (1.0 - 0.3 * suspicious_ratio)
        score = max(0.0, min(1.0, score))

        return LayerResult(
            name="embedding",
            score=score,
            weight=0.20,
            details={
                "avg_cosine_similarity": round(avg_sim, 4),
                "min_similarity": round(min_sim, 4),
                "max_similarity": round(max_sim, 4),
                "suspicious_pairs": suspicious_pairs,
                "total_pairs": n_pairs,
                "n_propositions": n,
                "embedder": embedder_name,
                "cosine_threshold": cosine_thresh,
                "sparsity_threshold": sparsity_thresh,
            }
        )
