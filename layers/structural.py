from coherence_engine.core.types import ArgumentStructure, LayerResult


class StructuralAnalyzer:
    def analyze(self, structure: ArgumentStructure) -> LayerResult:
        props = structure.propositions
        rels = structure.relations
        n = len(props)

        if n < 2:
            return LayerResult(
                name="structural",
                score=0.5,
                weight=0.15,
                details={"reason": "too few propositions"}
            )

        # Build adjacency (directed support/attack graph)
        adj = {p.id: set() for p in props}  # outgoing
        incoming = {p.id: set() for p in props}
        for r in rels:
            if r.source_id in adj:
                adj[r.source_id].add(r.target_id)
            if r.target_id in incoming:
                incoming[r.target_id].add(r.source_id)

        # 1. Connectivity: fraction of props reachable from claims
        claims = structure.claims
        if not claims:
            claims = [props[0]]  # Use first prop as root if no explicit claims

        reachable = set()
        for claim in claims:
            self._bfs(claim.id, adj, incoming, reachable)
        connectivity = len(reachable) / max(n, 1)

        # 2. Isolation: props with no relations at all
        connected_ids = set()
        for r in rels:
            connected_ids.add(r.source_id)
            connected_ids.add(r.target_id)
        isolated = sum(1 for p in props if p.id not in connected_ids)
        isolation_penalty = isolated / max(n, 1)

        # 3. Depth: longest chain
        max_depth = self._max_depth(props, adj)
        depth_factor = min(1.0, max_depth / 3.0)  # Normalize: depth 3+ is good

        # 4. Circularity: detect cycles via DFS
        n_cycles = self._count_cycles(props, adj)
        circularity_penalty = min(1.0, n_cycles * 0.2)  # Each cycle is a 0.2 penalty

        # Composite structural score
        score = connectivity * (1.0 - isolation_penalty) * (1.0 - circularity_penalty)
        score = score * (0.5 + 0.5 * depth_factor)  # Depth bonus
        score = max(0.0, min(1.0, score))

        n_support = sum(1 for r in rels if r.relation_type == "supports")
        n_attack = sum(1 for r in rels if r.relation_type == "attacks")

        return LayerResult(
            name="structural",
            score=score,
            weight=0.15,
            details={
                "connectivity": round(connectivity, 4),
                "isolation_penalty": round(isolation_penalty, 4),
                "max_depth": max_depth,
                "n_cycles": n_cycles,
                "n_support_relations": n_support,
                "n_attack_relations": n_attack,
                "n_isolated": isolated,
            }
        )

    def _bfs(self, start_id, adj, incoming, visited):
        """BFS from start, following both directions."""
        queue = [start_id]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            for neighbor in adj.get(node, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
            for neighbor in incoming.get(node, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

    def _max_depth(self, props, adj):
        """Find longest path in DAG (or max depth before cycle)."""
        max_d = 0
        for p in props:
            visited = set()
            d = self._dfs_depth(p.id, adj, visited)
            max_d = max(max_d, d)
        return max_d

    def _dfs_depth(self, node, adj, visited, depth=0):
        if node in visited or depth > 20:  # Cycle guard
            return depth
        visited.add(node)
        max_d = depth
        for neighbor in adj.get(node, set()):
            d = self._dfs_depth(neighbor, adj, visited, depth + 1)
            max_d = max(max_d, d)
        visited.discard(node)
        return max_d

    def _count_cycles(self, props, adj):
        """Count cycles via coloring DFS."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {p.id: WHITE for p in props}
        cycles = [0]

        def dfs(node):
            color[node] = GRAY
            for neighbor in adj.get(node, set()):
                if color.get(neighbor) == GRAY:
                    cycles[0] += 1
                elif color.get(neighbor) == WHITE:
                    dfs(neighbor)
            color[node] = BLACK

        for p in props:
            if color[p.id] == WHITE:
                dfs(p.id)
        return cycles[0]
