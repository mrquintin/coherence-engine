"""Domain-relative coherence comparison."""

from coherence_engine.domain.premises import DOMAINS, TENSIONS
from coherence_engine.domain.detector import DomainDetector
from coherence_engine.core.types import CoherenceResult


class DomainComparator:
    """Compare an argument's coherence against its domain's incumbent premises."""

    def __init__(self, embedder=None, scorer=None):
        self._detector = DomainDetector(embedder=embedder)
        self._scorer = scorer
        self._domain_score_cache = {}

    def _get_domain_coherence(self, domain_key: str, premises: list) -> float:
        """Score a domain's premises through the full pipeline, with caching."""
        if domain_key in self._domain_score_cache:
            return self._domain_score_cache[domain_key]

        if self._scorer is None:
            from coherence_engine.core.scorer import CoherenceScorer
            self._scorer = CoherenceScorer()

        text = " ".join(premises)
        try:
            result = self._scorer.score(text)
            score = result.composite_score
        except Exception:
            score = 0.5

        self._domain_score_cache[domain_key] = score
        return score

    def compare(self, result: CoherenceResult, domains: list = None) -> dict:
        """Compare coherence result against domain incumbents.

        Args:
            result: A CoherenceResult from the scorer.
            domains: Optional list of domain keys. Auto-detected if None.

        Returns:
            Dict with domain comparisons, tensions, and assessment.
        """
        structure = result.argument_structure
        if structure is None or structure.n_propositions < 2:
            return {"error": "Insufficient argument structure for comparison"}

        if domains is None:
            texts = [p.text for p in structure.propositions]
            detected = self._detector.detect(texts, top_k=3)
            domains = [key for key, score in detected]

        comparisons = []
        for domain_key in domains:
            if domain_key not in DOMAINS:
                continue

            domain_info = DOMAINS[domain_key]
            domain_premises = domain_info["premises"]

            domain_coherence = self._get_domain_coherence(
                domain_key, domain_premises
            )
            differential = result.composite_score - domain_coherence

            if differential > 0.1:
                assessment = "SUPERIOR"
            elif differential > -0.1:
                assessment = "COMPARABLE"
            else:
                assessment = "INFERIOR"

            comparisons.append({
                "domain": domain_key,
                "domain_name": domain_info["name"],
                "argument_coherence": round(result.composite_score, 4),
                "domain_coherence": round(domain_coherence, 4),
                "differential": round(differential, 4),
                "assessment": assessment,
            })

        relevant_tensions = []
        domain_set = set(domains)
        for d1, d2, description in TENSIONS:
            if d1 in domain_set or d2 in domain_set:
                relevant_tensions.append({
                    "domains": (d1, d2),
                    "description": description,
                })

        return {
            "comparisons": comparisons,
            "relevant_tensions": relevant_tensions,
            "detected_domains": domains,
        }
