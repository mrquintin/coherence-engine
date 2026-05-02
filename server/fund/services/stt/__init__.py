"""Speech-to-text adapter package.

The :mod:`stt` package provides a deterministic :class:`SpeechToText`
interface plus three pluggable backends: a local Whisper backend
(``whisper.cpp`` / ``faster_whisper`` via lazy import), Deepgram, and
AssemblyAI. The :func:`router.transcribe` entry point selects a primary
backend, falls back on transient failure or low average confidence, and
returns an :class:`STTResult` whose ``transcript`` matches
``coherence_engine.core.types.Transcript`` for downstream compilers.

See ``docs/specs/stt.md`` for cost-per-minute and language-fitness notes.
"""

from coherence_engine.server.fund.services.stt.interface import (
    LOW_STT_CONFIDENCE,
    SpeechToText,
    STTError,
    STTProvenance,
    STTResult,
    STTUnavailable,
    WhisperNotAvailable,
)
from coherence_engine.server.fund.services.stt.router import (
    STTRouter,
    transcribe,
)

__all__ = [
    "LOW_STT_CONFIDENCE",
    "SpeechToText",
    "STTError",
    "STTProvenance",
    "STTResult",
    "STTRouter",
    "STTUnavailable",
    "WhisperNotAvailable",
    "transcribe",
]
