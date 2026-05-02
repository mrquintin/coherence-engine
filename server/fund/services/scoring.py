"""Scoring orchestrator service using the real coherence engine."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Dict, Any, List

from sqlalchemy.orm import Session

from coherence_engine.config import EngineConfig
from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.domain.comparator import DomainComparator
from coherence_engine.server.fund.observability.otel import (
    get_tracer,
    safe_set_attributes,
)
from coherence_engine.server.fund.services.uncertainty import calibrated_superiority_interval_95
from coherence_engine.server.fund.services import transcript_quality as _transcript_quality


_LOG = logging.getLogger("coherence_engine.fund.scoring")


# Per-prompt-62: every paid embedding call we issue under scoring is
# recorded against the application via ``cost_telemetry.record_cost``.
# The default SKU is the small embedding model the in-process scorer
# uses for production runs; when the scorer reports a different
# embedder via ``model_versions.embedder``, callers may pass an
# explicit ``sku`` override.
DEFAULT_EMBEDDING_SKU = "openai.text-embedding-3-small.tokens"


def record_scoring_cost(
    db: Session,
    *,
    application_id: str,
    score_output: Dict[str, object],
    sku: str = DEFAULT_EMBEDDING_SKU,
) -> None:
    """Record the embedding-cost of a scoring run as a ``CostEvent``.

    ``units`` is the number of 1000-token buckets implied by the
    ``input_text`` the scorer actually consumed. The token count is
    estimated from observed character length (≈ 4 chars per token --
    OpenAI's standard heuristic) and is therefore *server-derived*
    rather than client-supplied (per prompt 62 prohibition: never
    trust client unit counts).

    The function silently returns when the SKU is not in the pricing
    registry rather than blocking the scoring path -- a pricing-table
    misconfiguration must not corrupt a decision.
    """
    from coherence_engine.server.fund.services.cost_telemetry import (
        compute_idempotency_key,
        record_cost,
    )
    from coherence_engine.server.fund.services.cost_pricing import (
        CostPricingError,
    )

    input_text = str(score_output.get("input_text") or "")
    if not input_text:
        return
    estimated_tokens = max(1.0, len(input_text) / 4.0)
    units_1k = estimated_tokens / 1000.0

    coherence_id = str(score_output.get("coherence_result_id") or "")
    discriminator = coherence_id or hashlib.sha256(
        input_text.encode("utf-8")
    ).hexdigest()[:24]
    idem = compute_idempotency_key(
        provider="openai",
        sku=sku,
        application_id=application_id,
        discriminator=discriminator,
    )
    try:
        record_cost(
            db,
            provider="openai",
            sku=sku,
            units=units_1k,
            application_id=application_id,
            idempotency_key=idem,
        )
    except CostPricingError as exc:
        _LOG.warning("scoring_cost_record_skipped sku=%s reason=%s", sku, exc)


_TRACER = get_tracer("coherence_engine.fund.services.scoring")
_SCORING_LAYER_NAMES = (
    "contradiction",
    "argumentation",
    "embedding",
    "compression",
    "structural",
)


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
        # No inline text: fall back to object storage if a coh:// URI is set.
        # Only ``coh://`` URIs are loaded from storage — legacy ``db://`` URIs
        # are placeholders and have always meant "use the inline column" so we
        # leave them alone to preserve existing behavior.
        uri = getattr(application, "transcript_uri", None)
        if uri and isinstance(uri, str) and uri.startswith("coh://"):
            try:
                streamed = _transcript_quality.load_transcript_text(uri)
                if streamed and streamed.strip():
                    return streamed.strip()
            except Exception:
                # Storage hiccups must not silently downgrade the score:
                # bubbling up here lets the scoring worker's retry path treat
                # this as a transient error.
                raise
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
        # Tracing (prompt 61): one parent span per scoring run, plus
        # one child span per coherence layer. The parent span records
        # the application id (no PII) and the input length so a slow
        # run can be correlated with the eligible-queue snapshot. PII
        # such as ``one_liner`` is intentionally NOT attached.
        application_id = getattr(application, "id", None) or getattr(
            application, "application_id", None
        )
        with _TRACER.start_as_current_span("score.application") as parent_span:
            safe_set_attributes(
                parent_span,
                {
                    "application.id": str(application_id) if application_id else None,
                    "domain.primary": getattr(application, "domain_primary", None),
                },
            )

            input_text = self._build_input_text(application, transcript_text_override)
            if not input_text:
                input_text = "No transcript provided."
            safe_set_attributes(
                parent_span, {"transcript.length_chars": len(input_text)}
            )

            with _TRACER.start_as_current_span("score.layer.composite") as composite_span:
                result = self._scorer.score(input_text)
                safe_set_attributes(
                    composite_span,
                    {"score.composite": float(result.composite_score)},
                )

            # Emit one explicit per-layer child span. The parent span
            # is still ``score.application`` since these layers are
            # already computed inside ``self._scorer.score``; the
            # spans serve as named anchors for layer-level latency
            # attribution in the trace UI.
            for layer in result.layer_results:
                layer_name = str(getattr(layer, "name", "unknown")).lower()
                with _TRACER.start_as_current_span(
                    f"score.layer.{layer_name}"
                ) as layer_span:
                    safe_set_attributes(
                        layer_span,
                        {
                            "score.layer.name": layer_name,
                            "score.layer.value": float(layer.score),
                        },
                    )

            domain_key = getattr(application, "domain_primary", "market_economics")
            with _TRACER.start_as_current_span("score.domain_comparator"):
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

            safe_set_attributes(
                parent_span,
                {
                    "score.absolute": absolute,
                    "score.baseline": baseline,
                    "score.superiority": superiority,
                    "score.contradictions": n_contradictions,
                },
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

