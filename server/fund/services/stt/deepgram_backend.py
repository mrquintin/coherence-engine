"""Deepgram managed STT backend.

The HTTP layer uses :mod:`urllib.request` so we do not pull in ``requests``
just for one POST. Tests substitute the transport via
:meth:`DeepgramBackend.set_transport_for_tests` rather than monkeypatching
the global module — keeps the test surface explicit.

Network errors, 5xx responses, and timeouts are converted to
:class:`STTError` so the router can decide whether to fall back. 4xx is
*not* converted — a malformed key or a missing audio file is a
configuration bug, not a transient one, and silently failing over masks
the bug.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Callable, Optional, Sequence

from coherence_engine.core.types import Transcript, TranscriptTurn
from coherence_engine.server.fund.services.stt.interface import (
    STTError,
    STTProvenance,
    STTResult,
    average_word_confidence,
    fetch_audio_bytes,
)


_LOG = logging.getLogger("coherence_engine.fund.stt.deepgram")
_DEFAULT_ENDPOINT = "https://api.deepgram.com/v1/listen"
_DEFAULT_TIMEOUT = 60.0


# Transport contract:
#   transport(url, *, body, headers, timeout) -> (status_code, response_bytes)
Transport = Callable[..., "tuple[int, bytes]"]


def _default_transport(url: str, *, body: bytes, headers: dict, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""
    except (urllib.error.URLError, TimeoutError) as exc:
        raise STTError(f"deepgram_transport_error: {exc!r}") from exc


class DeepgramBackend:
    """Deepgram /v1/listen wrapper. Sends raw bytes; receives JSON."""

    name = "deepgram"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = "nova-2",
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY", "")
        self.model = model
        self.endpoint = endpoint
        self.timeout = float(timeout)
        self._transport: Transport = _default_transport

    def set_transport_for_tests(self, transport: Transport) -> None:
        self._transport = transport

    def transcribe(
        self,
        audio_uri: str,
        *,
        language: Optional[str] = None,
        hints: Sequence[str] = (),
    ) -> STTResult:
        if not self.api_key:
            raise STTError("deepgram_api_key_unset")
        audio_bytes = fetch_audio_bytes(audio_uri)
        params = {
            "model": self.model,
            "smart_format": "true",
            "punctuate": "true",
            "utterances": "true",
            "diarize": "true",
            "language": language or "en",
        }
        if hints:
            # Deepgram "keywords" accepts repeated query params; we encode the
            # comma-joined form which is also accepted and round-trips through
            # urllib without a separate dependency.
            params["keywords"] = ",".join(hints)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.endpoint}?{query}"
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": _guess_content_type(audio_uri),
        }
        try:
            status, body = self._transport(
                url, body=audio_bytes, headers=headers, timeout=self.timeout
            )
        except STTError:
            raise
        except Exception as exc:  # defensive — unexpected transport bug
            raise STTError(f"deepgram_transport_unexpected: {exc!r}") from exc

        if status >= 500:
            raise STTError(f"deepgram_5xx status={status} body={body[:256]!r}")
        if status >= 400:
            # 4xx is not a transient error; surface it loudly.
            raise STTError(f"deepgram_4xx status={status} body={body[:256]!r}")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise STTError(f"deepgram_invalid_json: {exc!r}") from exc

        return _parse_deepgram_response(payload, audio_uri, self.model, language)


def _guess_content_type(audio_uri: str) -> str:
    lower = audio_uri.lower()
    if lower.endswith(".webm"):
        return "audio/webm"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".flac"):
        return "audio/flac"
    return "audio/wav"


def _parse_deepgram_response(
    payload: dict, audio_uri: str, model: str, language: Optional[str]
) -> STTResult:
    """Convert a Deepgram pre-recorded response to a normalized STTResult.

    Deepgram returns alternatives + per-utterance + per-word timing. We
    use ``utterances`` when present (gives us speaker labels for diarized
    requests), falling back to the first alternative's transcript when
    Deepgram returned a non-diarized response (older accounts).
    """
    results = (payload or {}).get("results") or {}
    detected_language = (
        ((results.get("channels") or [{}])[0].get("detected_language"))
        or language
        or "en"
    )

    utterances = results.get("utterances") or []
    turns = []
    words: list = []
    if utterances:
        for ti, utt in enumerate(utterances):
            speaker_idx = utt.get("speaker", 0)
            speaker = "founder" if int(speaker_idx) == 0 else "interviewer"
            for w in utt.get("words", ()) or ():
                words.append(
                    (
                        ti,
                        str(w.get("punctuated_word") or w.get("word") or "").strip(),
                        float(w.get("start", 0.0) or 0.0),
                        float(w.get("end", 0.0) or 0.0),
                        float(w.get("confidence", 0.0) or 0.0),
                    )
                )
            turns.append(
                TranscriptTurn(
                    speaker=speaker,
                    text=str(utt.get("transcript", "")).strip(),
                    confidence=float(utt.get("confidence", 0.0) or 0.0),
                    start_s=float(utt.get("start", 0.0) or 0.0),
                    end_s=float(utt.get("end", 0.0) or 0.0),
                )
            )
    else:
        # No utterances — fall back to the first channel/alternative.
        channel = (results.get("channels") or [{}])[0]
        alt = (channel.get("alternatives") or [{}])[0]
        for w in alt.get("words", ()) or ():
            words.append(
                (
                    0,
                    str(w.get("punctuated_word") or w.get("word") or "").strip(),
                    float(w.get("start", 0.0) or 0.0),
                    float(w.get("end", 0.0) or 0.0),
                    float(w.get("confidence", 0.0) or 0.0),
                )
            )
        if alt.get("transcript"):
            turns.append(
                TranscriptTurn(
                    speaker="founder",
                    text=str(alt["transcript"]).strip(),
                    confidence=float(alt.get("confidence", 0.0) or 0.0),
                    start_s=float(words[0][2]) if words else 0.0,
                    end_s=float(words[-1][3]) if words else 0.0,
                )
            )

    transcript = Transcript(
        session_id=_session_id_from_uri(audio_uri),
        language=str(detected_language),
        turns=tuple(turns),
        asr_model=f"deepgram:{model}",
    )
    avg_conf = (
        average_word_confidence(words)
        if words
        else (sum(t.confidence for t in turns) / len(turns) if turns else 0.0)
    )
    return STTResult(
        transcript=transcript,
        provenance=STTProvenance(
            stt_provider="deepgram",
            model=model,
            avg_confidence=float(avg_conf),
            word_count=len(words),
        ),
        words=tuple(words),
    )


def _session_id_from_uri(audio_uri: str) -> str:
    base = audio_uri.rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or "stt_session"
