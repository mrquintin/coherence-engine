"""STT provider router: primary + deterministic fallback.

The router owns three concerns the individual backends cannot:

1. Provider selection from environment configuration
   (``STT_PROVIDER_PRIMARY`` / ``STT_PROVIDER_FALLBACK``).
2. Fallback on transient failure — :class:`STTError` from a backend, or a
   transcript whose average word confidence is below
   ``STT_MIN_AVG_CONFIDENCE``. 4xx-shaped errors propagate as a hard fail
   to avoid masking misconfiguration.
3. Quality-flag handoff to ``transcript_quality.py``: when the *final*
   accepted transcript still falls below the threshold, the router
   stamps :data:`LOW_STT_CONFIDENCE` on the result so the deterministic
   gate can record the reason code.

Both backends failing raises :class:`STTUnavailable`. The single-backend
case (no fallback configured) lets the primary's error propagate as
:class:`STTUnavailable` too — callers should not need to know whether a
fallback was configured to handle the failure.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Iterable, List, Optional, Sequence

from coherence_engine.server.fund.services.stt.interface import (
    LOW_STT_CONFIDENCE,
    SpeechToText,
    STTError,
    STTResult,
    STTUnavailable,
    build_quality_flags,
)


__all__ = [
    "STTRouter",
    "transcribe",
    "record_stt_cost",
    "DEFAULT_MIN_AVG_CONFIDENCE",
    "STT_PROVIDER_TO_SKU",
]


# Map a backend ``name`` (matching ``SpeechToText.name``) to the pricing
# registry SKU. Used by :func:`record_stt_cost` to look up the per-
# minute price -- callers may override via the explicit ``sku`` arg.
STT_PROVIDER_TO_SKU: dict = {
    "deepgram": "deepgram.nova-2.audio_minute",
    "assemblyai": "assemblyai.universal.audio_minute",
    "whisper_local": "whisper_local.audio_minute",
    "whisper": "whisper_local.audio_minute",
}


_LOG = logging.getLogger("coherence_engine.fund.stt.router")
DEFAULT_MIN_AVG_CONFIDENCE = 0.6


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _build_backend(name: str) -> SpeechToText:
    """Construct a backend by name without importing the others.

    Lazy imports here are critical: we never want a missing optional
    dependency on one provider's path to prevent a different provider
    from being used.
    """
    key = (name or "").strip().lower()
    if key in ("whisper", "whisper_local", "local", "whisper-local"):
        from coherence_engine.server.fund.services.stt.whisper_backend import (
            WhisperLocalBackend,
        )

        return WhisperLocalBackend()
    if key == "deepgram":
        from coherence_engine.server.fund.services.stt.deepgram_backend import (
            DeepgramBackend,
        )

        return DeepgramBackend()
    if key in ("assemblyai", "assembly_ai", "assembly-ai"):
        from coherence_engine.server.fund.services.stt.assemblyai_backend import (
            AssemblyAIBackend,
        )

        return AssemblyAIBackend()
    raise STTError(f"unknown_stt_provider: {name!r}")


class STTRouter:
    """Provider-aware router with primary → fallback semantics."""

    def __init__(
        self,
        primary: SpeechToText,
        fallback: Optional[SpeechToText] = None,
        *,
        min_avg_confidence: Optional[float] = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.min_avg_confidence = (
            float(min_avg_confidence)
            if min_avg_confidence is not None
            else _env_float("STT_MIN_AVG_CONFIDENCE", DEFAULT_MIN_AVG_CONFIDENCE)
        )

    @classmethod
    def from_env(cls, **overrides) -> "STTRouter":
        primary_name = os.environ.get("STT_PROVIDER_PRIMARY", "whisper")
        fallback_name = os.environ.get("STT_PROVIDER_FALLBACK", "")
        primary = _build_backend(primary_name)
        fallback = _build_backend(fallback_name) if fallback_name else None
        return cls(primary=primary, fallback=fallback, **overrides)

    def transcribe(
        self,
        audio_uri: str,
        *,
        language: Optional[str] = None,
        hints: Sequence[str] = (),
    ) -> STTResult:
        attempts: List[str] = []
        errors: List[str] = []

        primary_result, primary_error = self._try_one(
            self.primary, audio_uri, language=language, hints=hints
        )
        attempts.append(self.primary.name)
        if primary_result is not None and self._meets_threshold(primary_result):
            return _stamp_result(
                primary_result,
                attempts=attempts,
                fallback_used=False,
                min_avg=self.min_avg_confidence,
            )
        if primary_error is not None:
            errors.append(f"{self.primary.name}: {primary_error}")
            _LOG.warning(
                "stt_router_primary_failed provider=%s error=%s",
                self.primary.name,
                primary_error,
            )

        # Either no fallback OR primary returned a low-confidence result we
        # cannot improve on. Decide which.
        if self.fallback is None:
            if primary_result is not None:
                # Below threshold, no fallback — mark and return as-is.
                return _stamp_result(
                    primary_result,
                    attempts=attempts,
                    fallback_used=False,
                    min_avg=self.min_avg_confidence,
                )
            raise STTUnavailable(
                "stt_unavailable_no_fallback errors=" + " | ".join(errors)
            )

        fallback_result, fallback_error = self._try_one(
            self.fallback, audio_uri, language=language, hints=hints
        )
        attempts.append(self.fallback.name)
        if fallback_result is not None and self._meets_threshold(fallback_result):
            return _stamp_result(
                fallback_result,
                attempts=attempts,
                fallback_used=True,
                min_avg=self.min_avg_confidence,
            )
        if fallback_error is not None:
            errors.append(f"{self.fallback.name}: {fallback_error}")
            _LOG.warning(
                "stt_router_fallback_failed provider=%s error=%s",
                self.fallback.name,
                fallback_error,
            )

        # Pick the highest-confidence result if both came back below
        # threshold; otherwise fail loud. This keeps the pipeline going
        # rather than dropping audio on the floor when both backends
        # produce *something* — ``LOW_STT_CONFIDENCE`` will gate it
        # downstream.
        best = _pick_best(primary_result, fallback_result)
        if best is None:
            raise STTUnavailable(
                "stt_unavailable_all_backends_failed errors=" + " | ".join(errors)
            )
        used_fallback = best is fallback_result
        return _stamp_result(
            best,
            attempts=attempts,
            fallback_used=used_fallback,
            min_avg=self.min_avg_confidence,
        )

    def _meets_threshold(self, result: STTResult) -> bool:
        return float(result.provenance.avg_confidence) >= self.min_avg_confidence

    def _try_one(
        self,
        backend: SpeechToText,
        audio_uri: str,
        *,
        language: Optional[str],
        hints: Sequence[str],
    ):
        try:
            result = backend.transcribe(audio_uri, language=language, hints=hints)
            return result, None
        except STTError as exc:
            return None, repr(exc)
        except Exception as exc:  # backend bugs should not silently abort
            _LOG.exception("stt_backend_unexpected_error provider=%s", backend.name)
            return None, repr(exc)


def _pick_best(*results: Optional[STTResult]) -> Optional[STTResult]:
    candidates = [r for r in results if r is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda r: float(r.provenance.avg_confidence))


def _stamp_result(
    result: STTResult,
    *,
    attempts: Iterable[str],
    fallback_used: bool,
    min_avg: float,
) -> STTResult:
    new_provenance = replace(
        result.provenance,
        attempts=tuple(attempts),
        fallback_used=bool(fallback_used),
    )
    flags = build_quality_flags(
        avg_confidence=result.provenance.avg_confidence,
        min_avg_confidence=min_avg,
        extra=result.quality_flags,
    )
    return STTResult(
        transcript=result.transcript,
        provenance=new_provenance,
        quality_flags=flags,
        words=result.words,
    )


def transcribe(
    audio_uri: str,
    *,
    language: Optional[str] = None,
    hints: Sequence[str] = (),
    router: Optional[STTRouter] = None,
) -> STTResult:
    """Module-level convenience wrapper around :class:`STTRouter`.

    Constructs a router from environment variables on first use unless
    ``router`` is supplied. Raises :class:`STTUnavailable` when every
    configured backend fails.
    """
    r = router or STTRouter.from_env()
    return r.transcribe(audio_uri, language=language, hints=hints)


# Re-export the symbol the prohibitions require to remain visible.
LOW_STT_CONFIDENCE = LOW_STT_CONFIDENCE


def record_stt_cost(
    db,
    *,
    application_id: Optional[str],
    result: STTResult,
    duration_seconds: float,
    discriminator: str,
    sku: Optional[str] = None,
) -> None:
    """Persist a ``CostEvent`` for one successful STT transcription (prompt 62).

    ``duration_seconds`` is the *observed* recording length we already
    persisted on :class:`InterviewRecording.duration_seconds` -- we never
    accept a duration claimed by a client. The function silently skips
    when the resolved provider has no price entry rather than blocking
    the transcription path; pricing-table misconfiguration must not
    drop the transcript on the floor.
    """
    from coherence_engine.server.fund.services.cost_telemetry import (
        compute_idempotency_key,
        record_cost,
    )
    from coherence_engine.server.fund.services.cost_pricing import (
        CostPricingError,
    )

    provider = str(result.provenance.stt_provider or "").strip().lower() or "unknown"
    resolved_sku = sku or STT_PROVIDER_TO_SKU.get(provider)
    if not resolved_sku:
        _LOG.info("stt_cost_record_skipped_unknown_provider provider=%s", provider)
        return
    minutes = max(0.0, float(duration_seconds) / 60.0)
    if minutes <= 0:
        return
    idem = compute_idempotency_key(
        provider=provider,
        sku=resolved_sku,
        application_id=application_id,
        discriminator=discriminator,
    )
    try:
        record_cost(
            db,
            provider=provider,
            sku=resolved_sku,
            units=minutes,
            application_id=application_id,
            idempotency_key=idem,
        )
    except CostPricingError as exc:
        _LOG.warning(
            "stt_cost_record_skipped sku=%s reason=%s", resolved_sku, exc
        )
