"""Layer 1 — Contradiction detection.

Research context: The Cosine Paradox (Experiment 1) showed that contradictions
have cosine similarity ~0.62 — nearly identical to entailments (~0.64). This
means we cannot detect contradictions via embedding similarity alone. Instead,
this layer uses NLI (when available) or aggressive heuristic pattern matching.
"""

import json
import os
import re
from typing import Optional

from coherence_engine.core.types import (
    Proposition, ContradictionPair, ArgumentStructure, LayerResult,
)


def _load_negation_patterns():
    """Load negation patterns from bundled JSON data file."""
    json_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "negation_patterns.json"
    )
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_PATTERNS_DATA = _load_negation_patterns()


class HeuristicContradictionDetector:
    """Detect contradictions via pattern matching and semantic heuristics.

    This is the zero-dependency fallback. It catches:
    1. Antonym pairs with shared context
    2. Explicit negation ("not", "never", "no")
    3. Numerical contradictions
    4. Commitment contradictions ("will" vs "will not", "always" vs "never")
    5. Sentiment polarity clashes (positive vs negative about same topic)
    """

    _INLINE_ANTONYM_PAIRS = [
        ("increase", "decrease"), ("increase", "reduce"),
        ("expand", "contract"), ("expand", "shrink"),
        ("improve", "worsen"), ("improve", "deteriorate"),
        ("grow", "decline"), ("grow", "shrink"),
        ("rise", "fall"), ("rise", "drop"),
        ("strengthen", "weaken"),
        ("support", "oppose"), ("support", "undermine"),
        ("accept", "reject"), ("approve", "deny"),
        ("allow", "prohibit"), ("allow", "prevent"), ("allow", "block"),
        ("create", "destroy"), ("build", "demolish"),
        ("include", "exclude"),
        ("safe", "dangerous"), ("safe", "unsafe"),
        ("good", "bad"), ("beneficial", "harmful"),
        ("positive", "negative"),
        ("success", "failure"), ("successful", "unsuccessful"),
        ("possible", "impossible"),
        ("legal", "illegal"),
        ("honest", "dishonest"),
        ("fair", "unfair"),
        ("sustainable", "unsustainable"),
        ("efficient", "inefficient"),
        ("consistent", "inconsistent"),
        ("coherent", "incoherent"),
        ("always", "never"),
        ("all", "none"), ("every", "no"),
        ("everyone", "nobody"), ("everything", "nothing"),
        ("true", "false"),
        ("agree", "disagree"),
        ("profit", "loss"),
        ("win", "lose"),
        ("open", "closed"),
        ("pro", "anti"),
        ("invest", "divest"),
        ("eco-friendly", "polluting"),
        ("renewable", "fossil"),
        ("reduce", "increase"), ("reducing", "increase"), ("reducing", "increasing"),
        ("commit", "abandon"), ("committed", "oppose"), ("committed", "never"),
        ("protect", "oppose"), ("protecting", "opposing"),
        ("sustainability", "oppose"), ("sustainable", "opposing"),
        ("priority", "oppose"), ("priority", "neglect"),
        ("prioritize", "neglect"),
        ("protect", "endanger"),
    ]

    _INLINE_NEGATION_WORDS = {
        "not", "no", "never", "neither", "nor", "don't", "doesn't",
        "won't", "wouldn't", "shouldn't", "couldn't", "cannot",
        "can't", "isn't", "aren't", "wasn't", "weren't", "haven't",
        "hasn't", "hadn't", "didn't", "without",
    }

    if _PATTERNS_DATA is not None:
        ANTONYM_PAIRS = [tuple(pair) for pair in _PATTERNS_DATA.get("antonym_pairs", [])] or _INLINE_ANTONYM_PAIRS
        NEGATION_WORDS = set(_PATTERNS_DATA.get("negation_words", [])) or _INLINE_NEGATION_WORDS
    else:
        ANTONYM_PAIRS = _INLINE_ANTONYM_PAIRS
        NEGATION_WORDS = _INLINE_NEGATION_WORDS

    COMMITMENT_POSITIVE = {"will", "shall", "must", "always", "committed", "plan",
                           "dedicated", "prioritize", "believe", "ensure", "guarantee"}
    COMMITMENT_NEGATIVE = {"will not", "won't", "never", "refuse", "reject",
                           "abandon", "not invest", "not plan", "not committed"}

    def __init__(self):
        # Build bidirectional antonym lookup
        self._antonym_map = {}
        for a, b in self.ANTONYM_PAIRS:
            self._antonym_map.setdefault(a.lower(), set()).add(b.lower())
            self._antonym_map.setdefault(b.lower(), set()).add(a.lower())

    def _tokenize(self, text: str) -> list:
        return re.findall(r'\b[\w\'-]+\b', text.lower())

    def _content_words(self, text: str) -> set:
        stopwords = {
            "the", "a", "an", "and", "or", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "up", "as", "is", "was", "are", "be",
            "been", "have", "has", "do", "does", "did", "this", "that", "it",
            "its", "we", "our", "they", "their", "he", "she", "his", "her",
        }
        tokens = self._tokenize(text)
        return {t for t in tokens if t not in stopwords and len(t) > 2}

    def _has_negation(self, text: str) -> bool:
        tokens = set(self._tokenize(text))
        return bool(tokens & self.NEGATION_WORDS)

    def _check_antonym_contradiction(self, text1: str, text2: str) -> Optional[str]:
        """Check if texts contain antonym pairs with shared topic."""
        words1 = set(self._tokenize(text1))
        words2 = set(self._tokenize(text2))

        for word in words1:
            if word in self._antonym_map:
                for antonym in self._antonym_map[word]:
                    if antonym in words2:
                        shared = self._content_words(text1) & self._content_words(text2)
                        if len(shared) >= 1:
                            return f"'{word}' vs '{antonym}' with shared context: {shared}"
        return None

    def _check_negation_contradiction(self, text1: str, text2: str) -> Optional[str]:
        """Check if one sentence negates the other's core claim."""
        neg1 = self._has_negation(text1)
        neg2 = self._has_negation(text2)

        # One negated, one not — and they share significant content
        if neg1 != neg2:
            content1 = self._content_words(text1)
            content2 = self._content_words(text2)
            shared = content1 & content2
            if len(shared) >= 2:
                return f"Negation with shared content: {shared}"

        return None

    def _check_commitment_contradiction(self, text1: str, text2: str) -> Optional[str]:
        """Check for conflicting commitments (will do X / will not do X)."""
        lower1 = text1.lower()
        lower2 = text2.lower()

        # Check if one has positive commitment and other has negative about same topic
        has_pos1 = any(w in lower1 for w in self.COMMITMENT_POSITIVE)
        has_neg2 = any(w in lower2 for w in self.COMMITMENT_NEGATIVE)
        has_pos2 = any(w in lower2 for w in self.COMMITMENT_POSITIVE)
        has_neg1 = any(w in lower1 for w in self.COMMITMENT_NEGATIVE)

        if (has_pos1 and has_neg2) or (has_pos2 and has_neg1):
            shared = self._content_words(text1) & self._content_words(text2)
            if len(shared) >= 1:
                return f"Commitment conflict with shared topic: {shared}"

        return None

    def _check_sentiment_clash(self, text1: str, text2: str) -> Optional[str]:
        """Check for positive vs negative sentiment about the same topic."""
        positive_words = {"committed", "priority", "value", "protect", "sustain",
                         "eco-friendly", "carbon neutral", "clean", "renewable",
                         "reduce", "reducing", "support", "invest", "improve"}
        negative_words = {"oppose", "increase coal", "never invest", "unnecessary",
                         "refuse", "reject", "won't", "will not", "never",
                         "abandon", "ignore", "neglect", "against"}

        lower1 = text1.lower()
        lower2 = text2.lower()

        pos1 = any(w in lower1 for w in positive_words)
        neg1 = any(w in lower1 for w in negative_words)
        pos2 = any(w in lower2 for w in positive_words)
        neg2 = any(w in lower2 for w in negative_words)

        if (pos1 and neg2) or (pos2 and neg1):
            shared = self._content_words(text1) & self._content_words(text2)
            if len(shared) >= 1:
                return f"Sentiment clash (positive vs negative) on: {shared}"

        return None

    def _check_numerical_contradiction(self, text1: str, text2: str) -> Optional[str]:
        """Check for numerical contradictions about same entity."""
        nums1 = re.findall(r'\d+(?:\.\d+)?%?', text1)
        nums2 = re.findall(r'\d+(?:\.\d+)?%?', text2)

        if nums1 and nums2:
            shared = self._content_words(text1) & self._content_words(text2)
            if len(shared) >= 2 and nums1[0] != nums2[0]:
                return f"Different numbers ({nums1[0]} vs {nums2[0]}) about {shared}"

        return None

    def detect(self, propositions: list) -> tuple:
        """Detect contradictions. Returns (consistency_score, contradictions_list)."""
        contradictions = []

        for i in range(len(propositions)):
            for j in range(i + 1, len(propositions)):
                p1 = propositions[i]
                p2 = propositions[j]

                explanation = None

                # Try each detection method
                explanation = explanation or self._check_antonym_contradiction(p1.text, p2.text)
                explanation = explanation or self._check_negation_contradiction(p1.text, p2.text)
                explanation = explanation or self._check_commitment_contradiction(p1.text, p2.text)
                explanation = explanation or self._check_sentiment_clash(p1.text, p2.text)
                explanation = explanation or self._check_numerical_contradiction(p1.text, p2.text)

                if explanation:
                    contradictions.append(ContradictionPair(
                        prop_a_id=p1.id,
                        prop_b_id=p2.id,
                        prop_a_text=p1.text,
                        prop_b_text=p2.text,
                        confidence=0.75,
                        explanation=explanation,
                    ))

        n_pairs = max(len(propositions) * (len(propositions) - 1) // 2, 1)
        score = max(0.0, 1.0 - (len(contradictions) / n_pairs))

        return (score, contradictions)


class NLIContradictionDetector:
    """Production-grade contradiction detection using DeBERTa NLI."""

    def __init__(self, model_name: str = "cross-encoder/nli-deberta-v3-large"):
        self._available = False
        self._model = None
        self._tokenizer = None

        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            self._model = AutoModelForSequenceClassification.from_pretrained(model_name)
            self._tokenizer = AutoTokenizer.from_pretrained(model_name)
            self._available = True
        except Exception:
            pass

    def detect(self, propositions: list) -> tuple:
        """Detect via NLI. Returns (consistency_score, contradictions_list)."""
        if not self._available:
            return (0.5, [])

        import torch

        contradictions = []
        total_weight = 0.0
        wcs = 0.0

        for i in range(len(propositions)):
            for j in range(i + 1, len(propositions)):
                p1 = propositions[i]
                p2 = propositions[j]

                inputs = self._tokenizer(
                    p1.text, p2.text,
                    return_tensors="pt", truncation=True, max_length=512,
                )
                with torch.no_grad():
                    logits = self._model(**inputs).logits
                    probs = torch.softmax(logits, dim=-1)

                # Label mapping from model config:
                # 0=contradiction, 1=entailment, 2=neutral
                contra_prob = probs[0, 0].item()
                weight = p1.importance * p2.importance
                total_weight += weight
                wcs += weight * contra_prob

                if contra_prob > 0.5:
                    contradictions.append(ContradictionPair(
                        prop_a_id=p1.id,
                        prop_b_id=p2.id,
                        prop_a_text=p1.text,
                        prop_b_text=p2.text,
                        confidence=contra_prob,
                        explanation=f"NLI P(contradiction)={contra_prob:.3f}",
                    ))

        score = 1.0 - (wcs / max(total_weight, 1e-9))
        return (max(0.0, min(1.0, score)), contradictions)


class ContradictionDetector:
    """Facade: tries NLI, falls back to heuristic."""

    def __init__(self, backend: str = "auto", nli_model: str = None):
        self._nli = None
        self._heuristic = HeuristicContradictionDetector()

        if backend in ("auto", "nli"):
            model = nli_model or "cross-encoder/nli-deberta-v3-large"
            nli = NLIContradictionDetector(model_name=model)
            if nli._available:
                self._nli = nli

    def analyze(self, structure: ArgumentStructure) -> LayerResult:
        """Run contradiction detection on argument structure."""
        if self._nli:
            score, contradictions = self._nli.detect(structure.propositions)
            backend = "nli"
        else:
            score, contradictions = self._heuristic.detect(structure.propositions)
            backend = "heuristic"

        return LayerResult(
            name="contradiction",
            score=score,
            weight=0.30,
            details={
                "n_contradictions": len(contradictions),
                "backend": backend,
                "contradiction_objects": contradictions,
                "contradictions": [
                    {"a": c.prop_a_text[:80], "b": c.prop_b_text[:80],
                     "confidence": round(c.confidence, 3), "explanation": c.explanation}
                    for c in contradictions
                ],
            },
        )
