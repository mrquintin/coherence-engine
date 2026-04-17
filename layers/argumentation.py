"""Layer 2 — Dung's Argumentation Framework.

Evaluates argument structure quality using Dung's abstract argumentation
framework. Computes the grounded extension (the unique minimal complete
extension) via iterative fixed-point — the most conservative judgment of
which propositions survive all attacks.
"""

from typing import Set
from coherence_engine.core.types import ArgumentStructure, LayerResult


class ArgumentationAnalyzer:
    """Analyze argument structure using Dung's argumentation framework."""

    def _build_attack_graph(self, structure: ArgumentStructure) -> dict:
        """Build directed graph of attack relations.

        Returns:
            Dict mapping prop_id -> set of prop_ids that this node attacks.
        """
        graph = {prop.id: set() for prop in structure.propositions}

        for rel in structure.relations:
            if rel.relation_type == "attacks":
                graph.setdefault(rel.source_id, set()).add(rel.target_id)

        return graph

    def _compute_grounded_extension(self, graph: dict) -> Set[str]:
        """Compute grounded extension via fixed-point iteration.

        The grounded extension is the least fixed point of the characteristic
        function. Start with unattacked arguments, then iteratively add
        arguments whose attackers are all defeated by the current set.
        """
        all_nodes = set(graph.keys())
        if not all_nodes:
            return set()

        attackers = {node: set() for node in all_nodes}
        for attacker, targets in graph.items():
            for target in targets:
                if target in attackers:
                    attackers[target].add(attacker)

        defended = set()

        for _ in range(len(all_nodes) + 1):
            new_defended = set()

            for node in all_nodes:
                node_attackers = attackers[node]

                if not node_attackers:
                    new_defended.add(node)
                else:
                    all_countered = all(
                        any(
                            d in graph and node_attacker in graph[d]
                            for d in defended
                        )
                        for node_attacker in node_attackers
                    )
                    if all_countered:
                        new_defended.add(node)

            if new_defended == defended:
                break
            defended = new_defended

        return defended

    def _detect_cycles(self, graph: dict) -> int:
        """Count back-edges (cycles) via DFS coloring."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {node: WHITE for node in graph}
        cycles = 0

        def dfs(node):
            nonlocal cycles
            color[node] = GRAY
            for neighbor in graph.get(node, set()):
                if color.get(neighbor) == GRAY:
                    cycles += 1
                elif color.get(neighbor) == WHITE:
                    dfs(neighbor)
            color[node] = BLACK

        for node in graph:
            if color[node] == WHITE:
                dfs(node)
        return cycles

    def analyze(self, structure: ArgumentStructure) -> LayerResult:
        """Run argumentation analysis and return LayerResult."""
        graph = self._build_attack_graph(structure)
        grounded = self._compute_grounded_extension(graph)

        n_attacks = sum(1 for r in structure.relations if r.relation_type == "attacks")
        n_supports = sum(1 for r in structure.relations if r.relation_type == "supports")
        n_cycles = self._detect_cycles(graph)

        total = len(structure.propositions)
        grounded_size = len(grounded)

        s2 = grounded_size / max(total, 1) if total > 0 else 1.0

        return LayerResult(
            name="argumentation",
            score=s2,
            weight=0.20,
            details={
                "n_propositions": total,
                "n_attack_relations": n_attacks,
                "n_support_relations": n_supports,
                "grounded_extension_size": grounded_size,
                "grounded_extension": sorted(grounded),
                "n_cycles": n_cycles,
                "total_relations": len(structure.relations),
            },
        )
