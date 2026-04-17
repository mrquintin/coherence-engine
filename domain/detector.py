"""Domain detection: identify which domain(s) an argument belongs to."""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Sequence

from coherence_engine.domain.premises import DOMAINS, get_domain_normative
from coherence_engine.embeddings.utils import cosine_similarity


class DomainDetector:
    """Detect the domain of an argument using embedding similarity."""

    def __init__(self, embedder=None):
        self._embedder = embedder
        self._domain_embeddings = None

    def _ensure_embedder(self):
        if self._embedder is None:
            from coherence_engine.embeddings.base import get_embedder
            self._embedder = get_embedder()

    def _build_domain_embeddings(self):
        """Pre-compute average embedding for each domain's premises."""
        if self._domain_embeddings is not None:
            return

        self._ensure_embedder()
        self._domain_embeddings = {}

        all_texts = []
        domain_keys = []
        for key, domain in DOMAINS.items():
            for premise in domain["premises"]:
                all_texts.append(premise)
                domain_keys.append(key)

        # Fit TF-IDF if needed
        if hasattr(self._embedder, 'fit') and not getattr(self._embedder, 'fitted', True):
            self._embedder.fit(all_texts)

        embeddings = self._embedder.embed_batch(all_texts)

        # Average embeddings per domain
        domain_embs = {}
        domain_counts = {}
        dim = len(embeddings[0]) if embeddings else 0

        for i, key in enumerate(domain_keys):
            if key not in domain_embs:
                domain_embs[key] = [0.0] * dim
                domain_counts[key] = 0
            for d in range(dim):
                domain_embs[key][d] += embeddings[i][d]
            domain_counts[key] += 1

        # Normalize
        for key in domain_embs:
            n = domain_counts[key]
            domain_embs[key] = [v / n for v in domain_embs[key]]

        self._domain_embeddings = domain_embs

    def detect(self, texts: list, top_k: int = 3) -> list:
        """Detect top-K matching domains for the given texts.

        Args:
            texts: List of proposition texts from the argument.
            top_k: Number of top domains to return.

        Returns:
            List of (domain_key, similarity_score) tuples, sorted descending.
        """
        self._build_domain_embeddings()

        # Compute average embedding of input texts
        if hasattr(self._embedder, 'fit') and not getattr(self._embedder, 'fitted', True):
            self._embedder.fit(texts)

        embeddings = self._embedder.embed_batch(texts)
        if not embeddings:
            return []

        dim = len(embeddings[0])
        avg_emb = [0.0] * dim
        for emb in embeddings:
            for d in range(dim):
                avg_emb[d] += emb[d]
        avg_emb = [v / len(embeddings) for v in avg_emb]

        # Compare against each domain
        scores = []
        for key, domain_emb in self._domain_embeddings.items():
            sim = cosine_similarity(avg_emb, domain_emb)
            scores.append((key, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


_WORD_RE = re.compile(r"[a-z]+")

_LEMMA_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "had", "has", "have", "he", "her", "his", "i", "if", "in", "is",
    "it", "its", "me", "my", "no", "of", "on", "or", "our", "out", "over",
    "she", "so", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "too", "under", "up", "was", "we", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your",
}


def _lemmas_from_texts(texts) -> set:
    """Return a set of normalized content tokens from an iterable of texts."""
    lemmas: set = set()
    for t in texts or ():
        if not t:
            continue
        for token in _WORD_RE.findall(t.lower()):
            if token and token not in _LEMMA_STOPWORDS:
                lemmas.add(token)
    return lemmas


def _ontology_graph_id(ontology) -> Optional[str]:
    if ontology is None:
        return None
    entities = getattr(ontology, "entities", ())
    edges = getattr(ontology, "edges", ())
    if not entities and not edges:
        return None
    parts = [getattr(ontology, "schema_version", "")]
    for e in entities:
        parts.append(f"{e.type}|{e.id}|{e.mentions}")
    for ed in edges:
        parts.append(f"{ed.src}|{ed.dst}|{ed.relation}|{ed.evidence_count}")
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def detect_domain_mix(
    propositions: Sequence,
    ontology,
    *,
    embedder=None,
    top_k: int = 3,
):
    """Fuse topical, premise, ontology, and normative signals into a DomainMix.

    Signals:
      - topic:     cosine between mean proposition embedding and per-domain
                   premise centroid (via the supplied or default embedder).
      - premise:   Jaccard between proposition lemmas and domain premise
                   lemmas.
      - ontology:  normalized count of ontology entity surface forms that
                   appear verbatim in each domain's premise keywords.
      - normative: cosine between the argument's NormativeProfile and each
                   domain's declared normative profile.

    Coefficients are 0.4/0.25/0.2/0.15 for topic/premise/ontology/normative;
    any individual signal that cannot be computed (missing embedder, empty
    ontology, empty text, etc.) is skipped, its coefficient is redistributed
    across the surviving signals, and a `signal_skipped: <name>` note is
    attached to the returned DomainMix.
    """
    from coherence_engine.core.types import DomainMix
    from coherence_engine.domain.normative import compute_normative_profile

    notes: list = []
    normative = compute_normative_profile(propositions or [])
    graph_id = _ontology_graph_id(ontology)

    domain_keys = list(DOMAINS.keys())
    per_domain_topic = {k: 0.0 for k in domain_keys}
    per_domain_premise = {k: 0.0 for k in domain_keys}
    per_domain_ontology = {k: 0.0 for k in domain_keys}
    per_domain_normative = {k: 0.0 for k in domain_keys}

    coefficients = {
        "topic": 0.4,
        "premise": 0.25,
        "ontology": 0.2,
        "normative": 0.15,
    }
    active = {k: True for k in coefficients}

    texts = [
        p.text for p in (propositions or [])
        if getattr(p, "text", "") and p.text.strip()
    ]

    # --- topic signal ---
    topic_ok = False
    if texts:
        try:
            emb = embedder
            if emb is None:
                from coherence_engine.embeddings.base import get_embedder
                emb = get_embedder()
            premise_texts: list = []
            premise_domain_keys: list = []
            for k, domain in DOMAINS.items():
                for premise in domain["premises"]:
                    premise_texts.append(premise)
                    premise_domain_keys.append(k)
            if hasattr(emb, "fit") and not getattr(emb, "fitted", True):
                emb.fit(premise_texts + texts)
            premise_embeds = emb.embed_batch(premise_texts)
            text_embeds = emb.embed_batch(texts)
            if premise_embeds and text_embeds:
                dim = len(premise_embeds[0])
                avg_text = [0.0] * dim
                for vec in text_embeds:
                    for d in range(dim):
                        avg_text[d] += vec[d]
                avg_text = [v / len(text_embeds) for v in avg_text]
                centroids: dict = {}
                counts: dict = {}
                for i, k in enumerate(premise_domain_keys):
                    if k not in centroids:
                        centroids[k] = [0.0] * dim
                        counts[k] = 0
                    vec = premise_embeds[i]
                    for d in range(dim):
                        centroids[k][d] += vec[d]
                    counts[k] += 1
                for k in centroids:
                    centroids[k] = [v / counts[k] for v in centroids[k]]
                for k in domain_keys:
                    if k in centroids:
                        sim = cosine_similarity(avg_text, centroids[k])
                        per_domain_topic[k] = max(0.0, sim)
                topic_ok = any(per_domain_topic[k] > 0.0 for k in domain_keys)
        except Exception:
            topic_ok = False
    if not topic_ok:
        active["topic"] = False
        notes.append("signal_skipped: topic")

    # --- premise signal (Jaccard on content lemmas) ---
    premise_ok = False
    arg_lemmas = _lemmas_from_texts(texts)
    if arg_lemmas:
        for k, domain in DOMAINS.items():
            dom_lemmas = _lemmas_from_texts(domain["premises"])
            if not dom_lemmas:
                continue
            inter = len(arg_lemmas & dom_lemmas)
            union = len(arg_lemmas | dom_lemmas)
            per_domain_premise[k] = inter / union if union else 0.0
        premise_ok = any(per_domain_premise[k] > 0.0 for k in domain_keys)
    if not premise_ok:
        active["premise"] = False
        notes.append("signal_skipped: premise")

    # --- ontology signal ---
    ontology_ok = False
    entity_surfaces: list = []
    if ontology is not None:
        for e in getattr(ontology, "entities", ()) or ():
            for sf in getattr(e, "surface_forms", ()) or ():
                s = sf.lower().strip()
                if s:
                    entity_surfaces.append(s)
    if entity_surfaces:
        raw_scores = {}
        for k, domain in DOMAINS.items():
            blob = " ".join(domain["premises"]).lower()
            count = 0
            for surface in entity_surfaces:
                pattern = r"\b" + re.escape(surface) + r"\b"
                if re.search(pattern, blob):
                    count += 1
            raw_scores[k] = count
        max_count = max(raw_scores.values()) if raw_scores else 0
        if max_count > 0:
            for k in domain_keys:
                per_domain_ontology[k] = raw_scores[k] / max_count
            ontology_ok = True
    if not ontology_ok:
        active["ontology"] = False
        notes.append("signal_skipped: ontology")

    # --- normative signal ---
    normative_ok = False
    arg_vec = [normative.rights, normative.utilitarian, normative.deontic]
    if any(v > 0.0 for v in arg_vec):
        for k in domain_keys:
            dom_vec = list(get_domain_normative(k))
            sim = cosine_similarity(arg_vec, dom_vec)
            per_domain_normative[k] = max(0.0, sim)
        normative_ok = any(per_domain_normative[k] > 0.0 for k in domain_keys)
    if not normative_ok:
        active["normative"] = False
        notes.append("signal_skipped: normative")

    active_coeffs = {k: coefficients[k] for k, on in active.items() if on}
    if not active_coeffs:
        return DomainMix(
            weights=(),
            normative=normative,
            ontology_graph_id=graph_id,
            notes=tuple(notes) if notes else None,
        )
    total_coeff = sum(active_coeffs.values())
    normalized_coeffs = {k: v / total_coeff for k, v in active_coeffs.items()}

    signals = {
        "topic": per_domain_topic,
        "premise": per_domain_premise,
        "ontology": per_domain_ontology,
        "normative": per_domain_normative,
    }
    final_scores = {}
    for k in domain_keys:
        score = 0.0
        for sig, coeff in normalized_coeffs.items():
            score += coeff * signals[sig][k]
        final_scores[k] = score

    ranked = sorted(final_scores.items(), key=lambda x: (-x[1], x[0]))
    k = max(1, int(top_k))
    top = ranked[:k]
    total = sum(s for _, s in top)
    if total <= 0.0:
        n = len(top)
        weights = tuple((key, 1.0 / n) for key, _ in top)
    else:
        weights = tuple((key, s / total) for key, s in top)

    return DomainMix(
        weights=weights,
        normative=normative,
        ontology_graph_id=graph_id,
        notes=tuple(notes) if notes else None,
    )
