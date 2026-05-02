"""Local Whisper backend (default).

Imports are deliberately deferred until :meth:`WhisperLocalBackend.transcribe`
is first called, so simply importing :mod:`stt` does not require
``faster-whisper`` (or the legacy ``whisper`` package, or the underlying
``ctranslate2`` / ``torch`` wheels) to be installed. Environments that have
neither library see :class:`WhisperNotAvailable` raised the first time they
try to transcribe — the router catches that and falls back to a managed
provider when one is configured.

The backend reads bytes through
:func:`coherence_engine.server.fund.services.object_storage.get` so a
local-filesystem URI, an ``s3://`` URI, and a Supabase Storage URI all
work without changes to call sites.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from coherence_engine.core.types import Transcript, TranscriptTurn
from coherence_engine.server.fund.services import object_storage as _object_storage
from coherence_engine.server.fund.services.stt.interface import (
    STTError,
    STTProvenance,
    STTResult,
    WhisperNotAvailable,
    average_word_confidence,
)


_LOG = logging.getLogger("coherence_engine.fund.stt.whisper")


@dataclass(frozen=True)
class _WhisperWord:
    text: str
    start: float
    end: float
    confidence: float


def _import_whisper_module():
    """Try faster_whisper, then whisper. Raise WhisperNotAvailable if neither."""
    try:
        import faster_whisper  # type: ignore

        return ("faster_whisper", faster_whisper)
    except Exception:  # pragma: no cover - exercised indirectly in tests
        pass
    try:
        import whisper  # type: ignore

        return ("whisper", whisper)
    except Exception:  # pragma: no cover - exercised indirectly in tests
        pass
    raise WhisperNotAvailable(
        "whisper_unavailable: install 'faster-whisper' (recommended) or 'openai-whisper' "
        "to use the local backend, or configure a managed provider via STT_PROVIDER_PRIMARY."
    )


def _fetch_audio_to_temp(audio_uri: str) -> str:
    """Materialize ``audio_uri`` to a temp file and return its path.

    Whisper variants only accept filesystem paths (the ctranslate2 reader
    insists on seekable file objects too); object-storage URIs must be
    rehydrated locally. The caller is responsible for unlinking the path.
    """
    if audio_uri.startswith("file://"):
        return audio_uri[len("file://") :]
    if os.path.isabs(audio_uri) and os.path.exists(audio_uri):
        return audio_uri
    data = _object_storage.get(audio_uri)
    fd, tmp_path = tempfile.mkstemp(prefix="stt_whisper_", suffix=".audio")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


class WhisperLocalBackend:
    """Local Whisper backend (default primary)."""

    name = "whisper"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        compute_type: str = "int8",
        device: str = "cpu",
    ) -> None:
        self.model_name = model or os.environ.get("WHISPER_MODEL", "base")
        self.compute_type = compute_type
        self.device = device
        self._model = None  # lazy
        self._flavor: Optional[str] = None  # "faster_whisper" | "whisper"

    # ------------------------------------------------------------------
    # Test seam — allow injecting an in-memory fake without monkeypatching
    # the import system. The fake must expose a ``transcribe(path, **kw)``
    # that returns ``(turns, words)`` where ``turns`` is a sequence of
    # objects with ``speaker``, ``text``, ``confidence``, ``start_s``,
    # ``end_s`` attributes and ``words`` matches the router contract.
    # ------------------------------------------------------------------
    def _set_fake_engine(self, fake) -> None:
        self._model = fake
        self._flavor = "fake"

    def _ensure_model(self):
        if self._model is not None:
            return
        flavor, module = _import_whisper_module()
        self._flavor = flavor
        if flavor == "faster_whisper":
            self._model = module.WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        else:
            self._model = module.load_model(self.model_name, device=self.device)

    def transcribe(
        self,
        audio_uri: str,
        *,
        language: Optional[str] = None,
        hints: Sequence[str] = (),
    ) -> STTResult:
        self._ensure_model()
        if self._flavor == "fake":
            turns, words = self._model.transcribe(
                audio_uri, language=language, hints=tuple(hints)
            )
            return self._assemble_result(audio_uri, turns, words)

        local_path = _fetch_audio_to_temp(audio_uri)
        try:
            if self._flavor == "faster_whisper":
                segments, info = self._model.transcribe(
                    local_path,
                    language=language,
                    word_timestamps=True,
                    initial_prompt=" ".join(hints) if hints else None,
                )
                turns, words = self._segments_to_turns_faster(segments)
                detected_language = getattr(info, "language", language) or "en"
            elif self._flavor == "whisper":
                result = self._model.transcribe(
                    local_path,
                    language=language,
                    word_timestamps=True,
                    initial_prompt=" ".join(hints) if hints else None,
                )
                turns, words = self._segments_to_turns_legacy(result.get("segments", ()))
                detected_language = result.get("language") or language or "en"
            else:  # pragma: no cover - defensive
                raise STTError(f"whisper_unknown_flavor: {self._flavor!r}")
        finally:
            if local_path != audio_uri and os.path.exists(local_path):
                try:
                    os.unlink(local_path)
                except OSError:
                    _LOG.debug("whisper_temp_cleanup_failed path=%s", local_path)

        return self._assemble_result(audio_uri, turns, words, language=detected_language)

    # ------------------------------------------------------------------
    # Segment → turn conversion. Whisper does not diarize; we emit a
    # single ``founder``-speaker turn per segment so transcript_compiler
    # has something to chew on. Diarization is handled by a future prompt.
    # ------------------------------------------------------------------
    @staticmethod
    def _segments_to_turns_faster(segments) -> Tuple[Tuple, Tuple]:
        turns = []
        words: list = []
        for ti, seg in enumerate(segments):
            seg_words = list(getattr(seg, "words", ()) or ())
            if seg_words:
                confs = [float(getattr(w, "probability", 1.0) or 1.0) for w in seg_words]
                avg = sum(confs) / len(confs)
                for w in seg_words:
                    words.append(
                        (
                            ti,
                            str(w.word).strip(),
                            float(w.start or 0.0),
                            float(w.end or 0.0),
                            float(getattr(w, "probability", 1.0) or 1.0),
                        )
                    )
            else:
                avg = float(getattr(seg, "avg_logprob", 0.0))
                avg = max(0.0, min(1.0, 1.0 + avg))  # rough log→prob clamp
            turns.append(
                TranscriptTurn(
                    speaker="founder",
                    text=str(seg.text).strip(),
                    confidence=float(avg),
                    start_s=float(seg.start or 0.0),
                    end_s=float(seg.end or 0.0),
                )
            )
        return tuple(turns), tuple(words)

    @staticmethod
    def _segments_to_turns_legacy(segments) -> Tuple[Tuple, Tuple]:
        turns = []
        words: list = []
        for ti, seg in enumerate(segments):
            seg_words = list(seg.get("words", ()) or ())
            if seg_words:
                confs = [float(w.get("probability", 1.0) or 1.0) for w in seg_words]
                avg = sum(confs) / len(confs)
                for w in seg_words:
                    words.append(
                        (
                            ti,
                            str(w.get("word", "")).strip(),
                            float(w.get("start", 0.0) or 0.0),
                            float(w.get("end", 0.0) or 0.0),
                            float(w.get("probability", 1.0) or 1.0),
                        )
                    )
            else:
                avg_logprob = float(seg.get("avg_logprob", 0.0) or 0.0)
                avg = max(0.0, min(1.0, 1.0 + avg_logprob))
            turns.append(
                TranscriptTurn(
                    speaker="founder",
                    text=str(seg.get("text", "")).strip(),
                    confidence=float(avg),
                    start_s=float(seg.get("start", 0.0) or 0.0),
                    end_s=float(seg.get("end", 0.0) or 0.0),
                )
            )
        return tuple(turns), tuple(words)

    def _assemble_result(
        self,
        audio_uri: str,
        turns,
        words,
        *,
        language: str = "en",
    ) -> STTResult:
        session_id = _session_id_from_uri(audio_uri)
        transcript = Transcript(
            session_id=session_id,
            language=language,
            turns=tuple(turns),
            asr_model=f"whisper:{self.model_name}",
        )
        avg_conf = average_word_confidence(words) if words else (
            sum(t.confidence for t in turns) / len(turns) if turns else 0.0
        )
        return STTResult(
            transcript=transcript,
            provenance=STTProvenance(
                stt_provider=self.name,
                model=self.model_name,
                avg_confidence=float(avg_conf),
                word_count=len(words),
            ),
            quality_flags=(),
            words=tuple(words),
        )


def _session_id_from_uri(audio_uri: str) -> str:
    """Best-effort session id derived from the URI tail; deterministic."""
    base = audio_uri.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or "stt_session"
