"""Lexical normative-profile extractor.

Computes a :class:`NormativeProfile` (rights / utilitarian / deontic) from a
set of propositions using simple normalized lemma-frequency counts against a
bundled JSON marker lexicon. No ML, no POS tagging.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Sequence

from coherence_engine.core.types import NormativeProfile, Proposition


_DEFAULT_MARKERS: dict = {
    "rights": ["rights", "freedom", "autonomy", "consent", "dignity"],
    "utilitarian": ["outcome", "utility", "welfare", "efficient", "aggregate"],
    "deontic": ["must", "ought", "obligation", "duty", "should"],
}

_WORD_RE = re.compile(r"[a-z]+")


def _markers_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "server", "fund", "data", "normative_markers.json")


def _load_markers() -> dict:
    try:
        with open(_markers_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {k: list(v) for k, v in _DEFAULT_MARKERS.items()}
    axes = data.get("axes")
    if not isinstance(axes, dict):
        return {k: list(v) for k, v in _DEFAULT_MARKERS.items()}
    merged = {}
    for axis, default in _DEFAULT_MARKERS.items():
        value = axes.get(axis)
        merged[axis] = list(value) if isinstance(value, list) and value else list(default)
    return merged


def _tokenize(text: str) -> list:
    if not text:
        return []
    return _WORD_RE.findall(text.lower())


def _count_matches(tokens: Sequence[str], markers: Iterable[str]) -> int:
    if not tokens:
        return 0
    marker_set = {m.lower() for m in markers if m}
    if not marker_set:
        return 0
    return sum(1 for t in tokens if t in marker_set)


def compute_normative_profile(propositions: Sequence[Proposition]) -> NormativeProfile:
    """Compute a :class:`NormativeProfile` from propositions.

    Each axis score is the share of tokens matching that axis's markers,
    clamped to [0, 1]. An empty input yields an all-zero profile.
    """
    if not propositions:
        return NormativeProfile(rights=0.0, utilitarian=0.0, deontic=0.0)

    markers = _load_markers()
    tokens: list = []
    for prop in propositions:
        tokens.extend(_tokenize(getattr(prop, "text", "") or ""))

    total = len(tokens)
    if total == 0:
        return NormativeProfile(rights=0.0, utilitarian=0.0, deontic=0.0)

    def _axis(name: str) -> float:
        count = _count_matches(tokens, markers.get(name, ()))
        return max(0.0, min(1.0, count / total))

    return NormativeProfile(
        rights=_axis("rights"),
        utilitarian=_axis("utilitarian"),
        deontic=_axis("deontic"),
    )
