from coherence_engine.core.types import CoherenceResult
from coherence_engine.core.explanation import ExplanationGenerator


class ReportGenerator:
    def to_text(self, result: CoherenceResult) -> str:
        """Generate a human-readable ASCII report."""
        composite = result.composite_score
        interpretation = self._interpret_score(composite)

        lines = [
            "═" * 50,
            " COHERENCE ENGINE — ANALYSIS REPORT",
            "═" * 50,
            f" Composite Score: {composite:.2f} / 1.00    {self._bar(composite)}",
            "─" * 50,
            " Layer Breakdown:",
        ]

        # Layer breakdown
        total_weighted = 0.0
        for layer in result.layer_results:
            bar = self._bar(layer.score)
            weighted = layer.score * layer.weight
            total_weighted += weighted
            lines.append(
                f"   {layer.name:15s} {bar} {layer.score:.2f}  (×{layer.weight:.2f} = {weighted:.3f})"
            )

        lines.extend([
            "─" * 50,
            f" Structure: {result.argument_structure.n_propositions} propositions "
            f"({len(result.argument_structure.claims)} claims, "
            f"{len(result.argument_structure.premises)} premises)",
            f" Contradictions: {len(result.contradictions)} detected",
        ])

        timings = result.metadata.get("layer_timings", {})
        if timings:
            lines.append("─" * 50)
            lines.append(" Layer Timing:")
            for name, secs in timings.items():
                lines.append(f"   {name:15s} {secs:.3f}s")
            total = result.metadata.get("elapsed_seconds", "N/A")
            lines.append(f"   {'total':15s} {total}s")

        lines.extend([
            "─" * 50,
            f" Interpretation: {interpretation}",
        ])

        explainer = ExplanationGenerator()
        explanations = explainer.explain(result)
        if explanations:
            lines.append("─" * 50)
            lines.append(" Explanations:")
            for item in explanations:
                lines.append(f"   • {item}")

        lines.append("═" * 50)

        return "\n".join(lines)

    def to_json(self, result: CoherenceResult) -> str:
        """Generate JSON representation."""
        return result.to_json()

    def to_markdown(self, result: CoherenceResult) -> str:
        """Generate markdown version with headers and a table."""
        lines = [
            "# Coherence Engine Analysis Report",
            "",
            f"**Composite Score:** {result.composite_score:.2f} / 1.00",
            "",
            f"**Interpretation:** {self._interpret_score(result.composite_score)}",
            "",
            "## Layer Breakdown",
            "",
            "| Layer | Score | Weight | Weighted |",
            "|-------|-------|--------|----------|",
        ]

        for layer in result.layer_results:
            weighted = layer.score * layer.weight
            lines.append(
                f"| {layer.name} | {layer.score:.3f} | {layer.weight:.2f} | {weighted:.4f} |"
            )

        lines.extend([
            "",
            "## Structure Details",
            "",
            f"- **Propositions:** {result.argument_structure.n_propositions}",
            f"- **Claims:** {len(result.argument_structure.claims)}",
            f"- **Premises:** {len(result.argument_structure.premises)}",
            f"- **Contradictions:** {len(result.contradictions)}",
            "",
            "## Metadata",
            "",
            f"- **Analysis Time:** {result.metadata.get('elapsed_seconds', 'N/A')}s",
            f"- **Embedder:** {result.metadata.get('embedder', 'none')}",
        ])

        timings = result.metadata.get("layer_timings", {})
        if timings:
            lines.extend([
                "",
                "### Layer Timing",
                "",
                "| Layer | Time (s) |",
                "|-------|----------|",
            ])
            for name, secs in timings.items():
                lines.append(f"| {name} | {secs:.3f} |")

        if result.contradictions:
            lines.extend([
                "",
                "## Detected Contradictions",
                "",
            ])
            for i, contradiction in enumerate(result.contradictions, 1):
                if hasattr(contradiction, 'to_dict'):
                    d = contradiction.to_dict()
                    lines.append(f"{i}. **{d.get('prop_a_text', 'Unknown')}** vs **{d.get('prop_b_text', 'Unknown')}**")
                    lines.append(f"   - Confidence: {d.get('confidence', 'N/A')}")
                    lines.append(f"   - Explanation: {d.get('explanation', 'N/A')}")
                else:
                    lines.append(f"{i}. {contradiction}")

        explainer = ExplanationGenerator()
        explanations = explainer.explain(result)
        if explanations:
            lines.extend(["", "## Explanations", ""])
            for i, item in enumerate(explanations, 1):
                lines.append(f"{i}. {item}")

        return "\n".join(lines)

    def _bar(self, score: float, width: int = 10) -> str:
        """Generate a filled/empty bar chart."""
        if not (0.0 <= score <= 1.0):
            score = max(0.0, min(1.0, score))
        filled = int(score * width)
        empty = width - filled
        return "[" + "█" * filled + "░" * empty + "]"

    def _interpret_score(self, score: float) -> str:
        """Interpret composite score into a human-readable category."""
        if score > 0.8:
            return "Highly Coherent"
        elif score > 0.6:
            return "Coherent"
        elif score > 0.4:
            return "Moderate"
        elif score > 0.2:
            return "Weak"
        else:
            return "Incoherent"
