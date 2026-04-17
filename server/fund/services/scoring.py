"""Scoring orchestrator service using the real coherence engine."""

from __future__ import annotations

import uuid
from typing import Dict, Any, List

from coherence_engine.config import EngineConfig
from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.domain.comparator import DomainComparator
from coherence_engine.server.fund.services.uncertainty import calibrated_superiority_interval_95


class ScoringService:
    """Runs real coherence scoring and returns fund decision features."""

    def __init__(self):
        # Force local CPU-safe backends to keep fund pipeline deterministic
        # and avoid runtime model downloads in API/worker paths.
        self._scorer = CoherenceScorer(
            EngineConfig(embedder="tfidf", contradiction_backend="heuristic")
        )
        self._comparator = DomainComparator(scorer=self._scorer)

    @staticmethod
    def detect_domain(one_liner: str) -> str:
        text = one_liner.lower()
        if any(k in text for k in ("policy", "government", "compliance", "regulation")):
            return "governance"
        if any(k in text for k in ("health", "medical", "clinical", "hospital")):
            return "public_health"
        return "market_economics"

    @staticmethod
    def _build_input_text(application: Any, transcript_text_override: str | None = None) -> str:
        if transcript_text_override and transcript_text_override.strip():
            return transcript_text_override.strip()
        if getattr(application, "transcript_text", None):
            return str(application.transcript_text)
        fallback_parts = [
            getattr(application, "one_liner", ""),
            getattr(application, "use_of_funds_summary", ""),
        ]
        return " ".join(p for p in fallback_parts if p).strip()

    @staticmethod
    def _extract_layer_scores(result) -> Dict[str, float]:
        scores = {
            "contradiction": 0.5,
            "argumentation": 0.5,
            "embedding": 0.5,
            "compression": 0.5,
            "structural": 0.5,
        }
        for layer in result.layer_results:
            key = str(layer.name).lower()
            if key in scores:
                scores[key] = round(float(layer.score), 6)
        return scores

    @staticmethod
    def _serialize_argument(result) -> Dict[str, List[Dict[str, object]]]:
        structure = result.argument_structure
        propositions = []
        relations = []
        if structure:
            for p in structure.propositions:
                propositions.append(
                    {
                        "id": p.id,
                        "text": p.text,
                        "type": p.prop_type,
                        "importance": round(float(p.importance), 6),
                    }
                )
            for r in structure.relations:
                relations.append(
                    {
                        "source_id": r.source_id,
                        "target_id": r.target_id,
                        "type": r.relation_type,
                        "strength": round(float(r.strength), 6),
                    }
                )
        return {"propositions": propositions, "relations": relations}

    def score_application(
        self,
        application: Any,
        transcript_text_override: str | None = None,
    ) -> Dict[str, object]:
        input_text = self._build_input_text(application, transcript_text_override)
        if not input_text:
            input_text = "No transcript provided."
        result = self._scorer.score(input_text)

        domain_key = getattr(application, "domain_primary", "market_economics")
        comparison = self._comparator.compare(result, domains=[domain_key])
        if comparison.get("comparisons"):
            baseline = float(comparison["comparisons"][0]["domain_coherence"])
        else:
            baseline = 0.5
        absolute = float(result.composite_score)
        superiority = absolute - baseline

        n_props = max(2, int(result.metadata.get("n_propositions", 2)))
        n_contradictions = len(result.contradictions)
        anti_gaming_score = float(result.metadata.get("anti_gaming_score", 1.0))
        anti_gaming_flags = list(result.metadata.get("anti_gaming_flags", []) or [])
        anti_gaming_metrics = dict(result.metadata.get("anti_gaming_metrics", {}) or {})
        transcript_quality = max(0.2, min(1.0, min(len(input_text) / 1200.0, 1.0)))

        layer_scores = self._extract_layer_scores(result)
        lower, upper, uncertainty_calibration = calibrated_superiority_interval_95(
            superiority=superiority,
            n_propositions=n_props,
            transcript_quality=transcript_quality,
            n_contradictions=n_contradictions,
            layer_scores=layer_scores,
        )

        argument = self._serialize_argument(result)
        contradiction_layer = next((lr for lr in result.layer_results if lr.name == "contradiction"), None)
        contradiction_backend = (
            contradiction_layer.details.get("backend", "unknown")
            if contradiction_layer and isinstance(contradiction_layer.details, dict)
            else "unknown"
        )

        return {
            "coherence_result_id": f"coh_{uuid.uuid4().hex[:12]}",
            "input_text": input_text,
            "absolute_coherence": round(absolute, 6),
            "baseline_coherence": round(baseline, 6),
            "coherence_superiority": round(superiority, 6),
            "coherence_superiority_ci95": {"lower": round(lower, 6), "upper": round(upper, 6)},
            "uncertainty_calibration": uncertainty_calibration,
            "layer_scores": layer_scores,
            "anti_gaming_score": round(anti_gaming_score, 6),
            "anti_gaming_flags": anti_gaming_flags,
            "anti_gaming_metrics": anti_gaming_metrics,
            "transcript_quality_score": round(transcript_quality, 6),
            "n_contradictions": n_contradictions,
            "model_versions": {
                "embedder": str(result.metadata.get("embedder", "unknown")),
                "contradiction_backend": contradiction_backend,
            },
            "argument": argument,
            "metadata_notes": [],
        }

