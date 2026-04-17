"""Explanation engine — human-readable explanations for low coherence scores."""

from coherence_engine.core.types import CoherenceResult


class ExplanationGenerator:
    """Generate human-readable explanations from a CoherenceResult.

    Reads per-layer details that already exist in LayerResult.details
    and formats them into actionable prose.
    """

    LOW_THRESHOLD = 0.5
    VERY_LOW_THRESHOLD = 0.3

    def explain(self, result: CoherenceResult) -> list:
        """Return a list of explanation strings, most important first."""
        explanations = []
        layer_map = {lr.name: lr for lr in result.layer_results}

        explanations.extend(self._explain_contradictions(result, layer_map))
        explanations.extend(self._explain_argumentation(layer_map))
        explanations.extend(self._explain_embedding(layer_map))
        explanations.extend(self._explain_compression(layer_map))
        explanations.extend(self._explain_structural(layer_map))

        if result.composite_score > 0.8 and not explanations:
            explanations.append("The text is highly coherent — no issues detected.")

        return explanations

    def explain_text(self, result: CoherenceResult) -> str:
        """Return explanations as a single formatted text block."""
        items = self.explain(result)
        if not items:
            return "No specific issues detected."
        lines = []
        for i, item in enumerate(items, 1):
            lines.append(f"  {i}. {item}")
        return "\n".join(lines)

    # ── Per-layer explanation helpers ─────────────────────────────

    def _explain_contradictions(self, result, layer_map):
        explanations = []
        lr = layer_map.get("contradiction")
        if lr is None:
            return explanations

        for c in result.contradictions:
            a = self._truncate(c.prop_a_text, 80)
            b = self._truncate(c.prop_b_text, 80)
            conf = f"{c.confidence:.0%}" if c.confidence else ""
            reason = f" ({c.explanation})" if c.explanation else ""
            explanations.append(
                f"Contradiction{reason}: \"{a}\" vs \"{b}\"{f' [{conf}]' if conf else ''}"
            )

        if lr.score < self.LOW_THRESHOLD and not result.contradictions:
            n = lr.details.get("n_contradictions", 0)
            if n:
                explanations.append(
                    f"{n} contradiction(s) detected — the text makes conflicting claims."
                )

        return explanations

    def _explain_argumentation(self, layer_map):
        explanations = []
        lr = layer_map.get("argumentation")
        if lr is None or lr.score >= self.LOW_THRESHOLD:
            return explanations

        details = lr.details
        total = details.get("n_propositions", 0)
        grounded = details.get("grounded_extension_size", 0)
        n_attacks = details.get("n_attack_relations", 0)
        cycles = details.get("n_cycles", 0)

        undefended = total - grounded
        if undefended > 0:
            grounded_ids = details.get("grounded_extension", [])
            defended_str = ", ".join(grounded_ids[:5])
            explanations.append(
                f"{undefended} of {total} propositions are not in the "
                f"grounded extension (defeated by attacks). "
                f"Defended: {defended_str}{'...' if len(grounded_ids) > 5 else ''}."
            )

        if cycles > 0:
            explanations.append(
                f"Circular reasoning detected: {cycles} cycle(s) in the attack graph."
            )

        if n_attacks == 0 and lr.score < self.VERY_LOW_THRESHOLD:
            explanations.append(
                "No attack relations found — the argument structure "
                "may lack critical engagement."
            )

        return explanations

    def _explain_embedding(self, layer_map):
        explanations = []
        lr = layer_map.get("embedding")
        if lr is None or lr.score >= self.LOW_THRESHOLD:
            return explanations

        details = lr.details
        avg_sim = details.get("avg_cosine_similarity", 0)
        suspicious = details.get("suspicious_pairs", 0)
        total_pairs = details.get("total_pairs", 1)

        if avg_sim < 0.3:
            explanations.append(
                f"Average embedding similarity is very low ({avg_sim:.2f}) — "
                f"the propositions appear topically scattered."
            )

        if suspicious > 0:
            ratio = suspicious / max(total_pairs, 1)
            explanations.append(
                f"{suspicious} suspicious pair(s) detected "
                f"({ratio:.0%} of pairs): high cosine similarity but high "
                f"difference-vector sparsity (potential hidden contradictions)."
            )

        return explanations

    def _explain_compression(self, layer_map):
        explanations = []
        lr = layer_map.get("compression")
        if lr is None or lr.score >= self.LOW_THRESHOLD:
            return explanations

        details = lr.details
        ratio = details.get("compression_ratio", 0)
        redundancy = details.get("redundancy", 0)

        if ratio > 0.95:
            explanations.append(
                "Joint compression ratio is near 1.0 — the propositions "
                "share very little structural commonality."
            )
        if redundancy > 0.6:
            explanations.append(
                f"High redundancy ({redundancy:.0%}) — the text may be "
                f"excessively repetitive."
            )

        return explanations

    def _explain_structural(self, layer_map):
        explanations = []
        lr = layer_map.get("structural")
        if lr is None or lr.score >= self.LOW_THRESHOLD:
            return explanations

        details = lr.details
        isolated = details.get("n_isolated", 0)
        connectivity = details.get("connectivity", 0)
        depth = details.get("max_depth", 0)
        cycles = details.get("n_cycles", 0)

        if isolated > 0:
            explanations.append(
                f"{isolated} proposition(s) have no supporting evidence "
                f"or connections to other claims."
            )

        if connectivity < 0.5:
            explanations.append(
                f"Only {connectivity:.0%} of propositions are reachable "
                f"from the main claims — the argument is fragmented."
            )

        if depth < 2:
            explanations.append(
                "The argument is very flat (max depth < 2) — "
                "claims lack layered support."
            )

        if cycles > 0:
            explanations.append(
                f"Circular reasoning: {cycles} cycle(s) detected in the "
                f"support/attack graph."
            )

        return explanations

    @staticmethod
    def _truncate(text: str, maxlen: int) -> str:
        if len(text) <= maxlen:
            return text
        return text[:maxlen - 3] + "..."
