"""Deterministic transcript quality gate.

Rejects degenerate transcripts before scoring is enqueued. Lexical and numeric
checks only — no ML or external dependencies.

The composite ``score`` in :class:`TranscriptQualityReport` is a weighted average
of five normalized sub-metrics. Each sub-metric is clamped to [0, 1] against its
threshold (1.0 means the threshold is met or exceeded) and combined with these
fixed weights, chosen so that founder-talk volume, ASR confidence, and topic
coverage carry the most signal:

    founder_words      0.30   - did the founder actually talk?
    avg_confidence     0.25   - is the ASR signal trustworthy on average?
    low_conf_ratio     0.10   - tail-risk on confidence
    turns              0.10   - was there real back-and-forth?
    topic_coverage     0.25   - did the conversation span the rubric?

``passed`` is independent of ``score``: a transcript passes only when *every*
threshold is met. ``score`` is provided for ranking/telemetry, not gating.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

from coherence_engine.config import TranscriptQualityThresholds
from coherence_engine.core.types import Transcript, TranscriptTurn


_FOUNDER_SPEAKER = "founder"
_LOW_CONFIDENCE_TURN_THRESHOLD = 0.6

_WEIGHTS: Dict[str, float] = {
    "founder_words": 0.30,
    "avg_confidence": 0.25,
    "low_conf_ratio": 0.10,
    "turns": 0.10,
    "topic_coverage": 0.25,
}

_TOPICS_PATH = Path(__file__).resolve().parent.parent / "data" / "interview_topics.json"

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True)
class TranscriptQualityReport:
    """Outcome of evaluating a transcript against the quality gate."""
    passed: bool
    score: float
    reason_codes: tuple
    metrics: dict = field(default_factory=dict)


@lru_cache(maxsize=1)
def _load_default_topics() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    with _TOPICS_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return tuple((topic, tuple(kw.lower() for kw in keywords)) for topic, keywords in raw.items())


def _normalize_topics(
    topics: Optional[Mapping[str, Iterable[str]]],
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    if topics is None:
        return _load_default_topics()
    return tuple((topic, tuple(kw.lower() for kw in keywords)) for topic, keywords in topics.items())


def _founder_turns(transcript: Transcript) -> Tuple[TranscriptTurn, ...]:
    return tuple(t for t in transcript.turns if t.speaker == _FOUNDER_SPEAKER)


def _count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _topic_coverage(
    founder_text_lower: str,
    topics: Tuple[Tuple[str, Tuple[str, ...]], ...],
) -> float:
    if not topics:
        return 1.0
    hits = 0
    for _topic, keywords in topics:
        if any(kw in founder_text_lower for kw in keywords):
            hits += 1
    return hits / len(topics)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def evaluate_transcript(
    transcript: Transcript,
    *,
    config: Optional[TranscriptQualityThresholds] = None,
    topics: Optional[Mapping[str, Iterable[str]]] = None,
) -> TranscriptQualityReport:
    """Evaluate ``transcript`` and return a deterministic quality report."""
    cfg = config or TranscriptQualityThresholds.from_env()
    topic_tuples = _normalize_topics(topics)

    all_turns = transcript.turns
    founder_turns = _founder_turns(transcript)
    founder_text = " ".join(t.text for t in founder_turns)
    founder_words = _count_words(founder_text)

    total_turns = len(all_turns)

    if all_turns:
        avg_confidence = sum(t.confidence for t in all_turns) / total_turns
        low_conf_count = sum(1 for t in all_turns if t.confidence < _LOW_CONFIDENCE_TURN_THRESHOLD)
        low_conf_ratio = low_conf_count / total_turns
    else:
        avg_confidence = 0.0
        low_conf_ratio = 1.0

    coverage = _topic_coverage(founder_text.lower(), topic_tuples)

    reasons = []
    if founder_words < cfg.min_founder_words:
        reasons.append("TQG_FOUNDER_WORDS_LOW")
    if total_turns < cfg.min_turns:
        reasons.append("TQG_TURNS_LOW")
    if avg_confidence < cfg.min_avg_confidence:
        reasons.append("TQG_ASR_CONFIDENCE_LOW")
    if low_conf_ratio > cfg.max_low_conf_ratio:
        reasons.append("TQG_LOW_CONFIDENCE_RATIO_HIGH")
    if coverage < cfg.min_topic_coverage:
        reasons.append("TQG_TOPIC_COVERAGE_LOW")

    norm = {
        "founder_words": _clamp01(founder_words / cfg.min_founder_words) if cfg.min_founder_words > 0 else 1.0,
        "avg_confidence": _clamp01(avg_confidence / cfg.min_avg_confidence) if cfg.min_avg_confidence > 0 else 1.0,
        "low_conf_ratio": _clamp01(1.0 - (low_conf_ratio / cfg.max_low_conf_ratio)) if cfg.max_low_conf_ratio > 0 else (1.0 if low_conf_ratio == 0 else 0.0),
        "turns": _clamp01(total_turns / cfg.min_turns) if cfg.min_turns > 0 else 1.0,
        "topic_coverage": _clamp01(coverage / cfg.min_topic_coverage) if cfg.min_topic_coverage > 0 else 1.0,
    }
    score = sum(norm[k] * _WEIGHTS[k] for k in _WEIGHTS)

    metrics = {
        "founder_words": float(founder_words),
        "total_turns": float(total_turns),
        "avg_confidence": float(avg_confidence),
        "low_confidence_ratio": float(low_conf_ratio),
        "topic_coverage": float(coverage),
    }

    return TranscriptQualityReport(
        passed=not reasons,
        score=float(score),
        reason_codes=tuple(reasons),
        metrics=metrics,
    )
