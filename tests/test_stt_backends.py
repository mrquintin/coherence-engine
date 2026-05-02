"""Backend tests for the STT package (prompt 40).

Covers the three backends with their HTTP layer mocked. The Whisper
backend is tested via its in-memory fake-engine seam — we never actually
load a Whisper model in CI — and we also assert that asking the Whisper
backend to transcribe when neither ``faster_whisper`` nor ``whisper`` is
importable surfaces :class:`WhisperNotAvailable` rather than a raw
``ImportError``.

The synthetic audio fixture lives in ``tests/fixtures/audio/short_clip.wav``
and is a 0.6s 16kHz mono sine + speech-shaped noise — enough bytes for
the object-storage adapter to round-trip without ever being decoded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import pytest

from coherence_engine.server.fund.services.stt.assemblyai_backend import (
    AssemblyAIBackend,
)
from coherence_engine.server.fund.services.stt.deepgram_backend import DeepgramBackend
from coherence_engine.server.fund.services.stt.interface import (
    STTError,
    STTResult,
    WhisperNotAvailable,
)
from coherence_engine.server.fund.services.stt.whisper_backend import (
    WhisperLocalBackend,
    _import_whisper_module,
)


_FIXTURE_AUDIO = (
    Path(__file__).resolve().parent / "fixtures" / "audio" / "short_clip.wav"
)


@pytest.fixture
def audio_uri():
    """Return a ``file://`` URI to the synthetic clip.

    Object storage's filesystem backend resolves ``file://`` URIs without
    extra wiring; backends that hit ``object_storage.get`` therefore work
    against the on-disk fixture.
    """
    assert _FIXTURE_AUDIO.exists(), "audio fixture missing — regenerate it"
    return f"file://{_FIXTURE_AUDIO.as_posix()}"


# ---------------------------------------------------------------------------
# Whisper backend
# ---------------------------------------------------------------------------


class _FakeWhisper:
    """Stand-in for the lazy-loaded whisper engine."""

    def transcribe(self, path, *, language=None, hints=()):
        from coherence_engine.core.types import TranscriptTurn

        turns = (
            TranscriptTurn(
                speaker="founder",
                text="hello world",
                confidence=0.85,
                start_s=0.0,
                end_s=1.0,
            ),
        )
        words = (
            (0, "hello", 0.0, 0.4, 0.9),
            (0, "world", 0.4, 1.0, 0.8),
        )
        return turns, words


def test_whisper_backend_with_fake_engine_returns_normalized_transcript(audio_uri):
    backend = WhisperLocalBackend(model="tiny")
    backend._set_fake_engine(_FakeWhisper())

    result: STTResult = backend.transcribe(audio_uri, language="en")

    assert result.provenance.stt_provider == "whisper"
    assert result.provenance.model == "tiny"
    assert result.provenance.word_count == 2
    assert pytest.approx(result.provenance.avg_confidence, rel=1e-6) == 0.85
    assert len(result.transcript.turns) == 1
    assert result.transcript.turns[0].text == "hello world"
    assert result.transcript.asr_model == "whisper:tiny"


def test_whisper_backend_lazy_import_raises_whisper_not_available(monkeypatch):
    # Hide every import path the lazy-import probe would use, so any later
    # ``import faster_whisper`` / ``import whisper`` raises ImportError.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "whisper", None)

    with pytest.raises(WhisperNotAvailable) as excinfo:
        _import_whisper_module()
    assert "install" in str(excinfo.value).lower()


def test_whisper_backend_transcribe_raises_whisper_not_available(audio_uri, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setitem(sys.modules, "whisper", None)
    backend = WhisperLocalBackend()
    with pytest.raises(WhisperNotAvailable):
        backend.transcribe(audio_uri)


# ---------------------------------------------------------------------------
# Deepgram backend
# ---------------------------------------------------------------------------


_DEEPGRAM_OK_BODY = json.dumps(
    {
        "results": {
            "channels": [{"detected_language": "en"}],
            "utterances": [
                {
                    "speaker": 0,
                    "transcript": "we are building a deterministic engine",
                    "confidence": 0.91,
                    "start": 0.0,
                    "end": 2.5,
                    "words": [
                        {"word": "we", "start": 0.0, "end": 0.2, "confidence": 0.9},
                        {"word": "are", "start": 0.2, "end": 0.4, "confidence": 0.9},
                        {
                            "word": "building",
                            "start": 0.4,
                            "end": 0.9,
                            "confidence": 0.95,
                        },
                    ],
                }
            ],
        }
    }
).encode("utf-8")


def test_deepgram_backend_parses_utterances(audio_uri):
    captured: List[dict] = []

    def fake_transport(url, *, body, headers, timeout):
        captured.append({"url": url, "headers": dict(headers), "body_len": len(body)})
        return 200, _DEEPGRAM_OK_BODY

    backend = DeepgramBackend(api_key="dg_xxx")
    backend.set_transport_for_tests(fake_transport)

    result = backend.transcribe(audio_uri, language="en", hints=["coherence"])

    assert result.provenance.stt_provider == "deepgram"
    assert result.provenance.word_count == 3
    assert result.transcript.language == "en"
    assert result.transcript.turns[0].speaker == "founder"
    assert "diarize=true" in captured[0]["url"]
    assert captured[0]["headers"]["Authorization"] == "Token dg_xxx"
    # Avg confidence == mean(0.9, 0.9, 0.95)
    assert pytest.approx(result.provenance.avg_confidence, rel=1e-3) == (
        0.9 + 0.9 + 0.95
    ) / 3


def test_deepgram_backend_5xx_raises_stt_error(audio_uri):
    def fake_transport(url, *, body, headers, timeout):
        return 503, b"upstream-broken"

    backend = DeepgramBackend(api_key="dg_xxx")
    backend.set_transport_for_tests(fake_transport)

    with pytest.raises(STTError) as excinfo:
        backend.transcribe(audio_uri)
    assert "deepgram_5xx" in str(excinfo.value)


def test_deepgram_backend_missing_api_key_raises(audio_uri):
    backend = DeepgramBackend(api_key="")
    with pytest.raises(STTError):
        backend.transcribe(audio_uri)


# ---------------------------------------------------------------------------
# AssemblyAI backend
# ---------------------------------------------------------------------------


def _aai_route(method: str, url: str):
    if method == "POST" and url.endswith("/upload"):
        return 200, json.dumps({"upload_url": "https://aai.example/audio.wav"}).encode()
    if method == "POST" and url.endswith("/transcript"):
        return 200, json.dumps({"id": "tx_abc"}).encode()
    if method == "GET" and url.endswith("/transcript/tx_abc"):
        return 200, json.dumps(
            {
                "status": "completed",
                "language_code": "en",
                "text": "deterministic engine",
                "confidence": 0.88,
                "utterances": [
                    {
                        "speaker": "A",
                        "text": "deterministic engine",
                        "confidence": 0.88,
                        "start": 0,
                        "end": 1500,
                        "words": [
                            {
                                "text": "deterministic",
                                "start": 0,
                                "end": 700,
                                "confidence": 0.9,
                            },
                            {
                                "text": "engine",
                                "start": 700,
                                "end": 1500,
                                "confidence": 0.86,
                            },
                        ],
                    }
                ],
            }
        ).encode()
    return 404, b"unmapped"


def test_assemblyai_backend_two_phase_flow(audio_uri):
    calls: List[dict] = []

    def fake_transport(method, url, *, body, headers, timeout):
        calls.append({"method": method, "url": url})
        return _aai_route(method, url)

    backend = AssemblyAIBackend(
        api_key="aai_xxx",
        poll_interval=0.0,
        sleep=lambda s: None,
    )
    backend.set_transport_for_tests(fake_transport)

    result = backend.transcribe(audio_uri, language="en")

    assert result.provenance.stt_provider == "assemblyai"
    assert result.provenance.word_count == 2
    # upload, submit, poll
    methods = [c["method"] for c in calls]
    assert methods[:3] == ["POST", "POST", "GET"]
    # ms → seconds conversion sanity check
    assert result.transcript.turns[0].end_s == pytest.approx(1.5, rel=1e-3)


def test_assemblyai_backend_job_error_raises(audio_uri):
    state = {"job_polled": False}

    def fake_transport(method, url, *, body, headers, timeout):
        if url.endswith("/upload"):
            return 200, json.dumps({"upload_url": "u"}).encode()
        if method == "POST" and url.endswith("/transcript"):
            return 200, json.dumps({"id": "tx"}).encode()
        # GET poll
        state["job_polled"] = True
        return 200, json.dumps({"status": "error", "error": "garbled audio"}).encode()

    backend = AssemblyAIBackend(
        api_key="aai_xxx",
        poll_interval=0.0,
        sleep=lambda s: None,
    )
    backend.set_transport_for_tests(fake_transport)

    with pytest.raises(STTError) as excinfo:
        backend.transcribe(audio_uri)
    assert state["job_polled"] is True
    assert "garbled" in str(excinfo.value)


def test_assemblyai_backend_5xx_on_upload_raises(audio_uri):
    def fake_transport(method, url, *, body, headers, timeout):
        return 503, b"down"

    backend = AssemblyAIBackend(
        api_key="aai_xxx",
        poll_interval=0.0,
        sleep=lambda s: None,
    )
    backend.set_transport_for_tests(fake_transport)

    with pytest.raises(STTError) as excinfo:
        backend.transcribe(audio_uri)
    assert "assemblyai_5xx" in str(excinfo.value)
