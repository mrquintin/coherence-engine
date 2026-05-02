"""Engine configuration with sensible defaults."""

import os
from dataclasses import dataclass


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class TranscriptQualityThresholds:
    """Thresholds for the deterministic transcript-quality gate.

    Override any field via environment variables prefixed with COHERENCE_TQG_:
      COHERENCE_TQG_MIN_FOUNDER_WORDS, COHERENCE_TQG_MIN_TURNS,
      COHERENCE_TQG_MIN_AVG_CONFIDENCE, COHERENCE_TQG_MAX_LOW_CONF_RATIO,
      COHERENCE_TQG_MIN_TOPIC_COVERAGE.
    """
    min_founder_words: int = 400
    min_turns: int = 20
    min_avg_confidence: float = 0.70
    max_low_conf_ratio: float = 0.15
    min_topic_coverage: float = 0.60

    @classmethod
    def from_env(cls) -> "TranscriptQualityThresholds":
        defaults = cls()
        return cls(
            min_founder_words=_env_int("COHERENCE_TQG_MIN_FOUNDER_WORDS", defaults.min_founder_words),
            min_turns=_env_int("COHERENCE_TQG_MIN_TURNS", defaults.min_turns),
            min_avg_confidence=_env_float("COHERENCE_TQG_MIN_AVG_CONFIDENCE", defaults.min_avg_confidence),
            max_low_conf_ratio=_env_float("COHERENCE_TQG_MAX_LOW_CONF_RATIO", defaults.max_low_conf_ratio),
            min_topic_coverage=_env_float("COHERENCE_TQG_MIN_TOPIC_COVERAGE", defaults.min_topic_coverage),
        )


@dataclass
class EngineConfig:
    """Configuration for the Coherence Engine.

    All fields have defaults — zero configuration needed for basic usage.
    """

    # Layer weights (must sum to 1.0)
    weight_contradiction: float = 0.30
    weight_argumentation: float = 0.20
    weight_embedding: float = 0.20
    weight_compression: float = 0.15
    weight_structural: float = 0.15

    # Embedding backend: "auto" tries SBERT then falls back to TF-IDF
    embedder: str = "auto"
    sbert_model: str = "all-mpnet-base-v2"
    device: str = "auto"  # "auto" | "cpu" | "cuda" | "mps"

    # Contradiction detection: "auto" tries NLI then falls back to heuristic
    contradiction_backend: str = "auto"
    nli_model: str = "cross-encoder/nli-deberta-v3-large"

    # Domain comparison (optional)
    enable_domain_comparison: bool = False

    # Output
    output_format: str = "text"  # "text" | "json" | "markdown"
    verbose: bool = False

    # Performance
    max_propositions: int = 200
    batch_size: int = 32

    @property
    def weights(self) -> dict:
        return {
            "contradiction": self.weight_contradiction,
            "argumentation": self.weight_argumentation,
            "embedding": self.weight_embedding,
            "compression": self.weight_compression,
            "structural": self.weight_structural,
        }

    def validate(self):
        total = sum(self.weights.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Layer weights must sum to 1.0, got {total:.3f}")
