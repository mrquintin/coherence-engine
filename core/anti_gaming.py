"""Deterministic anti-gaming detector for coherence scoring.

The detector computes a bounded, offline, lexical/numeric ``AntiGamingReport``
for a set of propositions. By convention:

    score = 1.0  -> pitch is clean (no anti-gaming signal fired)
    score = 0.0  -> pitch is heavily gamed (all signals fired with max weight)

The composite coherence scorer multiplies its raw composite by
``(0.5 + 0.5 * score)`` — a clean score (1.0) leaves the composite unchanged,
while a worst-case score (0.0) halves it. This guarantees the anti-gaming
stage can never *increase* the composite, and can never more than halve it.

Signals (all deterministic, offline, lexical/numeric):

    AG_TEMPLATE_OVERLAP           (weight 0.30)
        Max character-trigram Jaccard between any proposition and any
        ``templates`` entry exceeds 0.6.

    AG_REPETITIVE_FILLER          (weight 0.20)
        Mean pairwise TF-IDF cosine similarity across propositions exceeds
        0.85, indicating saturated self-similarity ("word salad" filler).

    AG_PRIOR_CORPUS_ECHO          (weight 0.25)
        Max character-trigram Jaccard against any ``prior_corpus`` entry
        exceeds 0.8 (founder/competitor pitch echo).

    AG_FLUENCY_WITHOUT_CONTENT    (weight 0.15)
        Ratio of "long, complex" propositions (>= 18 tokens with subordinating
        connector) to "content-bearing" propositions (contain metric markers
        or numerics) exceeds 3.0.

    AG_CONTRADICTION_DENIAL       (weight 0.25)
        Any proposition contains both a claim and its explicit negation
        inside a single sentence without a concession marker
        ("but", "however", ...).

Total weight sum (0.30 + 0.20 + 0.25 + 0.15 + 0.25 = 1.15) slightly exceeds
1.0 on purpose so that the ``clamp(1.0 - sum_weights, 0.0, 1.0)`` floor can
be reached only when multiple independent signals co-occur. Individual
signals never single-handedly drive the score to zero.

Backward compatibility: the detector is pure; no external state, no hidden
wall-clock reads, no ML dependencies. All imports are stdlib.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Sequence, Tuple

__all__ = [
    "AG_TEMPLATE_OVERLAP",
    "AG_REPETITIVE_FILLER",
    "AG_PRIOR_CORPUS_ECHO",
    "AG_FLUENCY_WITHOUT_CONTENT",
    "AG_CONTRADICTION_DENIAL",
    "FLAG_WEIGHTS",
    "AntiGamingReport",
    "detect_anti_gaming",
]


AG_TEMPLATE_OVERLAP = "AG_TEMPLATE_OVERLAP"
AG_REPETITIVE_FILLER = "AG_REPETITIVE_FILLER"
AG_PRIOR_CORPUS_ECHO = "AG_PRIOR_CORPUS_ECHO"
AG_FLUENCY_WITHOUT_CONTENT = "AG_FLUENCY_WITHOUT_CONTENT"
AG_CONTRADICTION_DENIAL = "AG_CONTRADICTION_DENIAL"


FLAG_WEIGHTS: Mapping[str, float] = {
    AG_TEMPLATE_OVERLAP: 0.30,
    AG_REPETITIVE_FILLER: 0.20,
    AG_PRIOR_CORPUS_ECHO: 0.25,
    AG_FLUENCY_WITHOUT_CONTENT: 0.15,
    AG_CONTRADICTION_DENIAL: 0.25,
}


_TEMPLATE_JACCARD_THRESHOLD = 0.60
_PRIOR_CORPUS_JACCARD_THRESHOLD = 0.80
_SELF_SIMILARITY_THRESHOLD = 0.85
_FLUENCY_RATIO_THRESHOLD = 3.0
_LONG_TOKEN_THRESHOLD = 18
_SUBORDINATING_MARKERS = frozenset(
    {
        "because",
        "although",
        "whereas",
        "while",
        "whilst",
        "since",
        "which",
        "who",
        "whom",
        "wherein",
        "thereby",
        "thereof",
        "notwithstanding",
        "insofar",
    }
)
_CONCESSION_MARKERS = frozenset(
    {"but", "however", "yet", "although", "though", "nonetheless", "still"}
)
_NEGATION_MARKERS = frozenset(
    {
        "not",
        "never",
        "no",
        "none",
        "nobody",
        "nothing",
        "cannot",
        "can't",
        "won't",
        "don't",
        "doesn't",
        "didn't",
        "isn't",
        "aren't",
        "wasn't",
        "weren't",
    }
)
_METRIC_MARKERS = frozenset(
    {
        "users",
        "customers",
        "revenue",
        "arr",
        "mrr",
        "gmv",
        "ltv",
        "cac",
        "churn",
        "retention",
        "growth",
        "conversion",
        "margin",
        "trial",
        "study",
        "rct",
        "patients",
        "cohort",
        "participants",
        "subjects",
        "reduction",
        "improvement",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NUMERIC_RE = re.compile(r"\d")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;]+")


@dataclass(frozen=True)
class AntiGamingReport:
    """Bundled result of the anti-gaming detector.

    Attributes:
        score: Clean-convention score in ``[0.0, 1.0]``. 1.0 is pristine.
        flags: Tuple of flag codes that triggered (ordered by first trigger).
        metrics: Dict of raw measurements (intended for audit logs / debugging);
            keys include ``template_overlap_max``, ``self_similarity_mean``,
            ``prior_corpus_overlap_max``, ``fluency_ratio``,
            ``contradiction_denial_count``.
    """

    score: float
    flags: Tuple[str, ...]
    metrics: Dict[str, float]


def detect_anti_gaming(
    propositions: Sequence,
    *,
    prior_corpus: Sequence[str] = (),
    templates: Sequence[str] = (),
) -> AntiGamingReport:
    """Run the deterministic anti-gaming detector.

    Args:
        propositions: A sequence of objects exposing ``.text`` (e.g.
            ``Proposition``). Empty sequences yield a clean report.
        prior_corpus: Previously-seen founder/competitor pitch texts.
        templates: Known canned-answer template texts.

    Returns:
        A frozen :class:`AntiGamingReport`.
    """
    texts = _proposition_texts(propositions)
    metrics: Dict[str, float] = {
        "template_overlap_max": 0.0,
        "self_similarity_mean": 0.0,
        "prior_corpus_overlap_max": 0.0,
        "fluency_ratio": 0.0,
        "contradiction_denial_count": 0.0,
    }

    if not texts:
        return AntiGamingReport(score=1.0, flags=(), metrics=metrics)

    flags: list = []

    template_max = _max_trigram_jaccard(texts, templates)
    metrics["template_overlap_max"] = round(template_max, 6)
    if template_max > _TEMPLATE_JACCARD_THRESHOLD:
        flags.append(AG_TEMPLATE_OVERLAP)

    prior_max = _max_trigram_jaccard(texts, prior_corpus)
    metrics["prior_corpus_overlap_max"] = round(prior_max, 6)
    if prior_max > _PRIOR_CORPUS_JACCARD_THRESHOLD:
        flags.append(AG_PRIOR_CORPUS_ECHO)

    self_sim = _mean_pairwise_tfidf_cosine(texts)
    metrics["self_similarity_mean"] = round(self_sim, 6)
    if self_sim > _SELF_SIMILARITY_THRESHOLD:
        flags.append(AG_REPETITIVE_FILLER)

    fluency_ratio = _fluency_without_content_ratio(texts)
    metrics["fluency_ratio"] = round(fluency_ratio, 6)
    if fluency_ratio > _FLUENCY_RATIO_THRESHOLD:
        flags.append(AG_FLUENCY_WITHOUT_CONTENT)

    denial_count = _contradiction_denial_count(texts)
    metrics["contradiction_denial_count"] = float(denial_count)
    if denial_count > 0:
        flags.append(AG_CONTRADICTION_DENIAL)

    penalty = sum(FLAG_WEIGHTS.get(f, 0.0) for f in flags)
    score = max(0.0, min(1.0, 1.0 - penalty))

    return AntiGamingReport(
        score=round(score, 6),
        flags=tuple(flags),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Feature helpers — all deterministic, offline.
# ---------------------------------------------------------------------------


def _proposition_texts(propositions: Sequence) -> Tuple[str, ...]:
    out = []
    for p in propositions:
        text = getattr(p, "text", None)
        if text is None and isinstance(p, str):
            text = p
        if not text:
            continue
        t = str(text).strip()
        if t:
            out.append(t)
    return tuple(out)


def _char_trigrams(s: str) -> set:
    norm = re.sub(r"\s+", " ", s.lower()).strip()
    if len(norm) < 3:
        return {norm} if norm else set()
    return {norm[i : i + 3] for i in range(len(norm) - 2)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _max_trigram_jaccard(texts: Iterable[str], corpus: Iterable[str]) -> float:
    corpus_grams = [g for g in (_char_trigrams(c) for c in corpus) if g]
    if not corpus_grams:
        return 0.0
    best = 0.0
    for t in texts:
        tg = _char_trigrams(t)
        if not tg:
            continue
        for cg in corpus_grams:
            j = _jaccard(tg, cg)
            if j > best:
                best = j
                if best >= 1.0:
                    return best
    return best


def _tokenize(text: str) -> Tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.lower()))


def _tfidf_vectors(texts: Sequence[str]) -> Tuple[Dict[str, float], ...]:
    """Compute length-normalized sublinear-TF vectors for a small text set.

    Because the "document set" here is just the N propositions being scored,
    in-corpus IDF is degenerate (terms appearing in every proposition would
    receive near-zero IDF and mask exactly the "repetitive filler" signal we
    want to detect). We therefore use sublinear TF with a stopword-aware
    floor and L2-normalize: this gives a true surface-form cosine similarity
    that saturates to 1.0 on identical strings and stays high on near-
    paraphrases, which is the intended behavior of the `AG_REPETITIVE_FILLER`
    threshold (>0.85).
    """
    tokenized = [_tokenize(t) for t in texts]
    vectors: list = []
    for toks in tokenized:
        if not toks:
            vectors.append({})
            continue
        tf: Dict[str, float] = {}
        for term in toks:
            tf[term] = tf.get(term, 0.0) + 1.0
        weights = {term: 1.0 + math.log(c) for term, c in tf.items()}
        norm = math.sqrt(sum(w * w for w in weights.values()))
        if norm > 0:
            weights = {term: w / norm for term, w in weights.items()}
        vectors.append(weights)
    return tuple(vectors)


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    s = 0.0
    for term, w in a.items():
        s += w * b.get(term, 0.0)
    return max(0.0, min(1.0, s))


def _mean_pairwise_tfidf_cosine(texts: Sequence[str]) -> float:
    if len(texts) < 2:
        return 0.0
    vectors = _tfidf_vectors(texts)
    total = 0.0
    count = 0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            total += _cosine(vectors[i], vectors[j])
            count += 1
    if count == 0:
        return 0.0
    return total / count


def _fluency_without_content_ratio(texts: Sequence[str]) -> float:
    """Ratio of long+complex propositions to content-bearing propositions."""
    long_complex = 0
    content_bearing = 0
    for t in texts:
        tokens = _tokenize(t)
        if not tokens:
            continue
        token_set = set(tokens)
        is_long = len(tokens) >= _LONG_TOKEN_THRESHOLD
        is_complex = bool(token_set & _SUBORDINATING_MARKERS)
        if is_long and is_complex:
            long_complex += 1
        has_metric = bool(token_set & _METRIC_MARKERS) or bool(_NUMERIC_RE.search(t))
        if has_metric:
            content_bearing += 1
    if content_bearing == 0:
        return float(long_complex) if long_complex > 0 else 0.0
    return long_complex / content_bearing


def _contradiction_denial_count(texts: Sequence[str]) -> int:
    """Count sentences that claim X and also negate X without a concession marker."""
    count = 0
    for text in texts:
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            tokens = _tokenize(sentence)
            if len(tokens) < 4:
                continue
            token_set = set(tokens)
            if token_set & _CONCESSION_MARKERS:
                continue
            if not (token_set & _NEGATION_MARKERS):
                continue
            content_tokens = [
                tok
                for tok in tokens
                if tok not in _NEGATION_MARKERS
                and tok not in _CONCESSION_MARKERS
                and len(tok) > 2
            ]
            content_counts: Dict[str, int] = {}
            for tok in content_tokens:
                content_counts[tok] = content_counts.get(tok, 0) + 1
            if any(c >= 2 for c in content_counts.values()):
                count += 1
    return count
