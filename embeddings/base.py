"""Factory function for embedder selection."""

from .sbert import SBERTEmbedder
from .tfidf import TFIDFEmbedder


def get_embedder(config=None):
    """Get an embedder instance respecting EngineConfig settings.

    Args:
        config: Optional EngineConfig. Reads embedder, sbert_model, device,
                and batch_size fields. Falls back to sensible defaults.

    Returns:
        Embedder instance (SBERTEmbedder or TFIDFEmbedder)
    """
    from coherence_engine.config import EngineConfig

    if config is None:
        config = EngineConfig()

    preference = getattr(config, "embedder", "auto")
    model_name = getattr(config, "sbert_model", "all-mpnet-base-v2")
    device = getattr(config, "device", "auto")
    batch_size = getattr(config, "batch_size", 32)

    if preference == "tfidf":
        return TFIDFEmbedder()

    if preference in ("auto", "sbert"):
        sbert = SBERTEmbedder(model_name=model_name, device=device,
                              batch_size=batch_size)
        if sbert.available:
            return sbert
        if preference == "sbert":
            import sys
            print("SBERTEmbedder requested but unavailable; falling back to TF-IDF.",
                  file=sys.stderr)

    return TFIDFEmbedder()
