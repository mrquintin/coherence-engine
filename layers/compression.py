import math
import zlib
from coherence_engine.core.types import ArgumentStructure, LayerResult


class CompressionAnalyzer:
    def _calibrate(self, raw_coherence: float, n_propositions: int) -> float:
        """Length-aware normalization.

        Short texts produce low raw coherence (few shared bytes to exploit).
        Long texts produce higher raw values. This sigmoid maps raw values to
        [0,1] in a way that is more stable across text lengths.
        """
        length_factor = min(1.0, n_propositions / 10.0)
        k = 8.0 + 4.0 * length_factor
        midpoint = 0.10 + 0.05 * (1.0 - length_factor)
        return 1.0 / (1.0 + math.exp(-k * (raw_coherence - midpoint)))

    def analyze(self, structure: ArgumentStructure) -> LayerResult:
        texts = [p.text for p in structure.propositions]
        if len(texts) < 2:
            return LayerResult(
                name="compression",
                score=0.5,
                weight=0.15,
                details={"reason": "too few propositions"}
            )

        individual_sizes = [len(zlib.compress(t.encode('utf-8'))) for t in texts]
        sum_individual = sum(individual_sizes)

        joint_text = "\n".join(texts)
        joint_size = len(zlib.compress(joint_text.encode('utf-8')))

        if sum_individual == 0:
            compression_coherence = 0.0
        else:
            compression_coherence = 1.0 - (joint_size / sum_individual)

        score = self._calibrate(compression_coherence, len(texts))
        score = max(0.0, min(1.0, score))

        redundancy = 1.0 - (joint_size / max(len(joint_text.encode('utf-8')), 1))

        return LayerResult(
            name="compression",
            score=score,
            weight=0.15,
            details={
                "compression_coherence": round(compression_coherence, 4),
                "joint_size": joint_size,
                "sum_individual_sizes": sum_individual,
                "compression_ratio": round(joint_size / max(sum_individual, 1), 4),
                "redundancy": round(redundancy, 4),
                "calibration": "sigmoid_length_aware",
            }
        )
