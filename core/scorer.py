import sys
import time
from coherence_engine.config import EngineConfig
from coherence_engine.core.types import CoherenceResult, LayerResult
from coherence_engine.core.parser import ArgumentParser


class CoherenceScorer:
    def __init__(self, config: EngineConfig = None):
        self.config = config or EngineConfig()
        self.config.validate()
        self.parser = ArgumentParser(max_propositions=self.config.max_propositions)

        self._layers = None
        self._embedder = None

    def _init_layers(self):
        """Initialize all analysis layers and embedder."""
        if self._layers is not None:
            return

        from coherence_engine.layers.contradiction import ContradictionDetector
        from coherence_engine.layers.argumentation import ArgumentationAnalyzer
        from coherence_engine.layers.embedding import EmbeddingCoherenceAnalyzer
        from coherence_engine.layers.compression import CompressionAnalyzer
        from coherence_engine.layers.structural import StructuralAnalyzer
        from coherence_engine.embeddings.base import get_embedder

        self._embedder = get_embedder(config=self.config)

        self._layers = [
            ContradictionDetector(
                backend=self.config.contradiction_backend,
                nli_model=self.config.nli_model,
            ),
            ArgumentationAnalyzer(),
            EmbeddingCoherenceAnalyzer(embedder=self._embedder),
            CompressionAnalyzer(),
            StructuralAnalyzer(),
        ]

    def _verbose(self, msg: str):
        if self.config.verbose:
            print(f"  [coherence-engine] {msg}", file=sys.stderr)

    def score(
        self,
        text: str,
        *,
        anti_gaming: bool = True,
        prior_corpus=(),
        templates=(),
    ) -> CoherenceResult:
        """Score the coherence of a text. Returns CoherenceResult.

        Args:
            text: Raw input text.
            anti_gaming: If True (default), run the deterministic anti-gaming
                detector and apply a bounded penalty to the composite score.
                Backward-compatible: callers that omit the flag get the
                detector enabled by default, but a clean report never reduces
                the composite (multiplier is 1.0 at clean_score=1.0).
            prior_corpus: Optional sequence of previously-seen pitch texts
                passed to the anti-gaming detector. Omitted by default to
                preserve the existing single-argument call signature.
            templates: Optional sequence of known canned-answer templates
                passed to the anti-gaming detector.
        """
        start = time.time()

        self._init_layers()

        self._verbose("Parsing text into argument structure...")
        structure = self.parser.parse(text)
        parse_time = time.time() - start
        self._verbose(
            f"Parsed {structure.n_propositions} propositions "
            f"({len(structure.claims)} claims, {len(structure.premises)} premises) "
            f"in {parse_time:.3f}s"
        )

        if structure.n_propositions < 2:
            return CoherenceResult(
                composite_score=0.0,
                argument_structure=structure,
                metadata={
                    "error": "Text too short — need at least 2 propositions",
                    "anti_gaming_score": 1.0,
                    "anti_gaming_flags": [],
                    "anti_gaming_metrics": {},
                },
            )

        weights = self.config.weights
        weight_keys = ["contradiction", "argumentation", "embedding", "compression", "structural"]
        layer_names = [
            "Contradiction Detection (NLI/heuristic)",
            "Argumentation Analysis (Dung's framework)",
            "Embedding Coherence (cosine + sparsity)",
            "Compression Coherence (zlib proxy)",
            "Structural Analysis (graph quality)",
        ]

        results = []
        all_contradictions = []
        layer_timings = {}

        for i, layer in enumerate(self._layers):
            self._verbose(f"Running Layer {i+1}: {layer_names[i]}...")
            layer_start = time.time()
            try:
                result = layer.analyze(structure)
                result.weight = weights[weight_keys[i]]
                results.append(result)

                if result.name == "contradiction" and "contradictions" in result.details:
                    all_contradictions = result.details.get("contradiction_objects", [])
            except Exception as e:
                results.append(LayerResult(
                    name=weight_keys[i],
                    score=0.5,
                    weight=weights[weight_keys[i]],
                    warnings=[f"Layer failed: {e!s}"]
                ))

            layer_elapsed = time.time() - layer_start
            layer_timings[weight_keys[i]] = round(layer_elapsed, 3)
            self._verbose(
                f"  Layer {i+1} done: score={results[-1].score:.3f} "
                f"(weight={results[-1].weight:.2f}) in {layer_elapsed:.3f}s"
            )

        self._verbose("Running cross-layer signal fusion...")
        self._apply_fusion(results)

        raw_composite = sum(r.score * r.weight for r in results)
        raw_composite = max(0.0, min(1.0, raw_composite))

        anti_gaming_score = 1.0
        anti_gaming_flags: tuple = ()
        anti_gaming_metrics: dict = {}
        if anti_gaming:
            try:
                from coherence_engine.core.anti_gaming import detect_anti_gaming

                ag_report = detect_anti_gaming(
                    structure.propositions,
                    prior_corpus=tuple(prior_corpus),
                    templates=tuple(templates),
                )
                anti_gaming_score = float(ag_report.score)
                anti_gaming_flags = tuple(ag_report.flags)
                anti_gaming_metrics = dict(ag_report.metrics)
            except Exception as exc:
                self._verbose(f"anti_gaming detector failed: {exc}")
                anti_gaming_score = 1.0
                anti_gaming_flags = ()
                anti_gaming_metrics = {"error": 1.0}

        multiplier = 0.5 + 0.5 * anti_gaming_score
        composite = max(0.0, min(1.0, raw_composite * multiplier))

        elapsed = time.time() - start
        self._verbose(
            f"Composite score: {composite:.4f} "
            f"(raw={raw_composite:.4f}, anti_gaming={anti_gaming_score:.3f}, "
            f"flags={list(anti_gaming_flags)}; total {elapsed:.3f}s)"
        )

        return CoherenceResult(
            composite_score=composite,
            layer_results=results,
            argument_structure=structure,
            contradictions=all_contradictions,
            metadata={
                "elapsed_seconds": round(elapsed, 3),
                "n_propositions": structure.n_propositions,
                "n_claims": len(structure.claims),
                "n_premises": len(structure.premises),
                "embedder": type(self._embedder).__name__ if self._embedder else "none",
                "layer_timings": layer_timings,
                "raw_composite_score": round(raw_composite, 6),
                "anti_gaming_score": round(anti_gaming_score, 6),
                "anti_gaming_flags": list(anti_gaming_flags),
                "anti_gaming_metrics": anti_gaming_metrics,
            }
        )

    def _apply_fusion(self, results):
        """Cross-layer signal fusion: adjust scores when layers corroborate."""
        layer_map = {r.name: r for r in results}
        fusion_notes = []

        contra = layer_map.get("contradiction")
        embed = layer_map.get("embedding")
        if contra and embed:
            contra_pairs = set()
            for c in contra.details.get("contradiction_objects", []):
                contra_pairs.add((getattr(c, "prop_a_id", ""), getattr(c, "prop_b_id", "")))

            suspicious = embed.details.get("suspicious_pairs", 0)
            if contra_pairs and suspicious > 0:
                boost = min(0.05, 0.02 * len(contra_pairs))
                contra.score = max(0.0, contra.score - boost)
                fusion_notes.append(
                    f"L1+L3 corroboration: {len(contra_pairs)} contradictions "
                    f"confirmed by {suspicious} suspicious embedding pairs "
                    f"(contradiction score penalty +{boost:.3f})"
                )

        arg = layer_map.get("argumentation")
        struct = layer_map.get("structural")
        if arg and struct:
            grounded = set(arg.details.get("grounded_extension", []))
            n_isolated = struct.details.get("n_isolated", 0)
            if grounded and n_isolated > 0:
                penalty = min(0.05, 0.015 * n_isolated)
                struct.score = max(0.0, struct.score - penalty)
                fusion_notes.append(
                    f"L2+L5 conflict: {len(grounded)} grounded props but "
                    f"{n_isolated} isolated (structural penalty +{penalty:.3f})"
                )

        for r in results:
            if not r.details:
                r.details = {}
            r.details["fusion_notes"] = fusion_notes

    def score_file(self, path: str) -> CoherenceResult:
        """Score a text file."""
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        return self.score(text)
