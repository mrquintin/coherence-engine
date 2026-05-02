"""SpeechToText interface, errors, and result envelope (prompt 40).

The package returns an :class:`STTResult` rather than a bare
:class:`coherence_engine.core.types.Transcript` because ``Transcript`` is
frozen and has no ``metadata`` slot. ``STTResult`` carries the canonical
``Transcript`` plus an :class:`STTProvenance` record (provider, model,
average confidence, fallback chain) and a tuple of quality flags. The
router records ``LOW_STT_CONFIDENCE`` here for ``transcript_quality`` to
consume; downstream callers that only need the transcript read
``result.transcript``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, Sequence, Tuple

from coherence_engine.core.types import Transcript


# Quality flag constant — re-exported and consumed by transcript_quality.
LOW_STT_CONFIDENCE = "LOW_STT_CONFIDENCE"


class STTError(Exception):
    """Base class for STT failures."""


class STTUnavailable(STTError):
    """Raised when the router exhausts every configured backend."""


class WhisperNotAvailable(STTError):
    """Raised when neither faster_whisper nor whisper is importable.

    The Whisper backend deliberately lazy-imports its native dependency so
    that the package is usable in environments where the heavy ML wheels
    are not installed; callers that hit this error should either install
    ``faster-whisper`` or fall back to a managed provider.
    """


@dataclass(frozen=True)
class STTProvenance:
    """Provenance metadata about which backend produced a transcript."""

    stt_provider: str
    model: str
    avg_confidence: float
    word_count: int
    attempts: Tuple[str, ...] = ()
    fallback_used: bool = False


@dataclass(frozen=True)
class STTResult:
    """Container returned by :class:`SpeechToText.transcribe`.

    ``transcript`` is the canonical normalized output (per-word confidence
    is folded into per-turn confidence to match
    :class:`coherence_engine.core.types.TranscriptTurn`). Per-word detail
    is preserved in ``words`` for callers that need it (alignment, UI
    highlights). ``quality_flags`` is the set of deterministic gate codes
    consumed by ``transcript_quality.evaluate_transcript``.
    """

    transcript: Transcript
    provenance: STTProvenance
    quality_flags: Tuple[str, ...] = ()
    # Per-word records: (turn_index, word, start_s, end_s, confidence)
    words: Tuple[Tuple[int, str, float, float, float], ...] = ()


class SpeechToText(Protocol):
    """The protocol every STT backend implements.

    Backends are stateless from the caller's POV: ``transcribe`` is the
    only contract. Backends MUST raise :class:`STTError` (or a subclass)
    on hard failure so the router can decide whether to fall back.
    Returning a low-confidence transcript is *not* a hard failure — the
    router applies the configured ``STT_MIN_AVG_CONFIDENCE`` threshold
    after the call returns and may itself escalate to the fallback.
    """

    name: str

    def transcribe(
        self,
        audio_uri: str,
        *,
        language: Optional[str] = None,
        hints: Sequence[str] = (),
    ) -> STTResult:
        ...


def fetch_audio_bytes(audio_uri: str) -> bytes:
    """Resolve ``audio_uri`` to bytes, falling through to object storage.

    ``file://`` URIs and absolute filesystem paths are read directly
    (used by tests against on-disk fixtures and by single-machine
    deployments). Everything else delegates to
    :func:`object_storage.get`.
    """
    import os as _os

    if audio_uri.startswith("file://"):
        with open(audio_uri[len("file://") :], "rb") as fh:
            return fh.read()
    if _os.path.isabs(audio_uri) and _os.path.exists(audio_uri):
        with open(audio_uri, "rb") as fh:
            return fh.read()
    from coherence_engine.server.fund.services import object_storage as _obj

    return _obj.get(audio_uri)


def average_word_confidence(
    words: Iterable[Tuple[int, str, float, float, float]],
) -> float:
    """Compute the deterministic mean confidence across all words.

    Returns ``0.0`` when ``words`` is empty so the gate behaves as
    "fail closed" — an empty transcript is, by definition, low-confidence.
    """
    total = 0.0
    count = 0
    for _ti, _w, _s, _e, conf in words:
        total += float(conf)
        count += 1
    if count == 0:
        return 0.0
    return total / count


def build_quality_flags(
    avg_confidence: float,
    *,
    min_avg_confidence: float,
    extra: Iterable[str] = (),
) -> Tuple[str, ...]:
    """Translate router-level signals into transcript quality flags.

    Kept here (not in the router) so a backend that already knows its
    confidence is below threshold can attach the flag itself before the
    router re-evaluates — useful for debugging which provider produced
    the gating signal.
    """
    flags = list(extra)
    if avg_confidence < float(min_avg_confidence) and LOW_STT_CONFIDENCE not in flags:
        flags.append(LOW_STT_CONFIDENCE)
    return tuple(flags)
