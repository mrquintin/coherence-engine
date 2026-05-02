"""Router tests for the STT package (prompt 40).

Covers:
* primary success path (no fallback invoked).
* primary 5xx-style failure → fallback wins.
* low-confidence primary → fallback wins.
* low-confidence everywhere → returns best with LOW_STT_CONFIDENCE flag.
* both backends fail → STTUnavailable raised.
* env-driven construction picks the right pair of backends.
* unknown provider name raises STTError.
"""

from __future__ import annotations

from typing import Optional, Sequence

import pytest

from coherence_engine.core.types import Transcript, TranscriptTurn
from coherence_engine.server.fund.services.stt import (
    LOW_STT_CONFIDENCE,
    STTProvenance,
    STTResult,
    STTRouter,
    STTUnavailable,
)
from coherence_engine.server.fund.services.stt.interface import STTError
from coherence_engine.server.fund.services.stt.router import _build_backend


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_result(provider: str, *, avg: float, words: int = 4) -> STTResult:
    return STTResult(
        transcript=Transcript(
            session_id="sess",
            language="en",
            turns=(
                TranscriptTurn(
                    speaker="founder",
                    text="hello world",
                    confidence=avg,
                    start_s=0.0,
                    end_s=1.0,
                ),
            ),
            asr_model=f"{provider}:test",
        ),
        provenance=STTProvenance(
            stt_provider=provider,
            model="test",
            avg_confidence=avg,
            word_count=words,
        ),
    )


class _FakeBackend:
    def __init__(self, name: str, *, result=None, error=None):
        self.name = name
        self._result = result
        self._error = error
        self.calls = 0

    def transcribe(
        self,
        audio_uri: str,
        *,
        language: Optional[str] = None,
        hints: Sequence[str] = (),
    ) -> STTResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_router_primary_success_no_fallback_invoked():
    primary = _FakeBackend("primary", result=_make_result("primary", avg=0.9))
    fallback = _FakeBackend("fallback", result=_make_result("fallback", avg=0.95))
    r = STTRouter(primary=primary, fallback=fallback, min_avg_confidence=0.6)

    result = r.transcribe("file://x.wav")

    assert result.provenance.stt_provider == "primary"
    assert result.provenance.fallback_used is False
    assert result.provenance.attempts == ("primary",)
    assert LOW_STT_CONFIDENCE not in result.quality_flags
    assert primary.calls == 1
    assert fallback.calls == 0


def test_router_primary_5xx_falls_back_to_secondary():
    primary = _FakeBackend("primary", error=STTError("deepgram_5xx status=503"))
    fallback = _FakeBackend("fallback", result=_make_result("fallback", avg=0.85))
    r = STTRouter(primary=primary, fallback=fallback, min_avg_confidence=0.6)

    result = r.transcribe("file://x.wav")

    assert result.provenance.stt_provider == "fallback"
    assert result.provenance.fallback_used is True
    assert result.provenance.attempts == ("primary", "fallback")
    assert primary.calls == 1
    assert fallback.calls == 1


def test_router_primary_low_confidence_triggers_fallback():
    primary = _FakeBackend("primary", result=_make_result("primary", avg=0.30))
    fallback = _FakeBackend("fallback", result=_make_result("fallback", avg=0.90))
    r = STTRouter(primary=primary, fallback=fallback, min_avg_confidence=0.6)

    result = r.transcribe("file://x.wav")

    assert result.provenance.stt_provider == "fallback"
    assert result.provenance.fallback_used is True
    assert LOW_STT_CONFIDENCE not in result.quality_flags


def test_router_both_low_confidence_returns_best_with_flag():
    primary = _FakeBackend("primary", result=_make_result("primary", avg=0.40))
    fallback = _FakeBackend("fallback", result=_make_result("fallback", avg=0.55))
    r = STTRouter(primary=primary, fallback=fallback, min_avg_confidence=0.6)

    result = r.transcribe("file://x.wav")

    # Best of the two (fallback @ 0.55) survives, but the gate flag is set.
    assert result.provenance.stt_provider == "fallback"
    assert LOW_STT_CONFIDENCE in result.quality_flags
    assert result.provenance.fallback_used is True


def test_router_both_fail_raises_stt_unavailable():
    primary = _FakeBackend("primary", error=STTError("primary_5xx"))
    fallback = _FakeBackend("fallback", error=STTError("fallback_timeout"))
    r = STTRouter(primary=primary, fallback=fallback, min_avg_confidence=0.6)

    with pytest.raises(STTUnavailable) as excinfo:
        r.transcribe("file://x.wav")

    msg = str(excinfo.value)
    assert "primary" in msg and "fallback" in msg


def test_router_no_fallback_low_confidence_returns_with_flag():
    primary = _FakeBackend("primary", result=_make_result("primary", avg=0.20))
    r = STTRouter(primary=primary, fallback=None, min_avg_confidence=0.6)

    result = r.transcribe("file://x.wav")

    assert result.provenance.stt_provider == "primary"
    assert LOW_STT_CONFIDENCE in result.quality_flags
    assert result.provenance.fallback_used is False


def test_router_no_fallback_primary_error_raises():
    primary = _FakeBackend("primary", error=STTError("boom"))
    r = STTRouter(primary=primary, fallback=None, min_avg_confidence=0.6)

    with pytest.raises(STTUnavailable):
        r.transcribe("file://x.wav")


def test_router_from_env_uses_env_provider_names(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER_PRIMARY", "deepgram")
    monkeypatch.setenv("STT_PROVIDER_FALLBACK", "assemblyai")
    monkeypatch.setenv("STT_MIN_AVG_CONFIDENCE", "0.42")

    r = STTRouter.from_env()
    assert r.primary.name == "deepgram"
    assert r.fallback is not None and r.fallback.name == "assemblyai"
    assert r.min_avg_confidence == pytest.approx(0.42)


def test_router_from_env_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("STT_PROVIDER_PRIMARY", "carrier_pigeon")
    with pytest.raises(STTError):
        STTRouter.from_env()


def test_build_backend_aliases_resolve():
    assert _build_backend("whisper").name == "whisper"
    assert _build_backend("deepgram").name == "deepgram"
    assert _build_backend("assemblyai").name == "assemblyai"
    assert _build_backend("assembly-ai").name == "assemblyai"
