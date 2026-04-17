"""
The Coherence Engine — Measuring Logical Structure Through Embedding Geometry

A multi-layer analysis pipeline that scores the internal logical coherence
of any body of text on a continuous 0-to-1 scale.

Usage:
    from coherence_engine import CoherenceScorer
    scorer = CoherenceScorer()
    result = scorer.score("Your text here...")
    print(result.composite_score)
    print(result.report())
"""

__version__ = "2.0.0"
__author__ = "Michael"

from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.core.types import CoherenceResult, LayerResult
from coherence_engine.config import EngineConfig

__all__ = ["CoherenceScorer", "CoherenceResult", "LayerResult", "EngineConfig"]
