"""AssemblyAI managed STT backend.

AssemblyAI's "v2" API is two-step: upload bytes → POST a transcript job.
The polling step is intentionally bounded — at default-budget defaults a
single ``transcribe`` call takes at most ``poll_max_seconds``; on
exhaustion the backend raises :class:`STTError` and the router gets a
chance to fall back rather than blocking a worker indefinitely.
"""

from __future__ import annotations

import json
import logging
import os
import time
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


_LOG = logging.getLogger("coherence_engine.fund.stt.assemblyai")
_DEFAULT_BASE = "https://api.assemblyai.com/v2"
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_POLL_INTERVAL = 1.0
_DEFAULT_POLL_MAX_SECONDS = 300.0


# Transport contract:
#   transport(method, url, *, body, headers, timeout) -> (status_code, response_bytes)
Transport = Callable[..., "tuple[int, bytes]"]


def _default_transport(
    method: str, url: str, *, body: Optional[bytes], headers: dict, timeout: float
):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""
    except (urllib.error.URLError, TimeoutError) as exc:
        raise STTError(f"assemblyai_transport_error: {exc!r}") from exc


class AssemblyAIBackend:
    """AssemblyAI v2 client."""

    name = "assemblyai"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        poll_max_seconds: float = _DEFAULT_POLL_MAX_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("ASSEMBLYAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.poll_max_seconds = float(poll_max_seconds)
        self._sleep = sleep
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
            raise STTError("assemblyai_api_key_unset")

        audio_bytes = fetch_audio_bytes(audio_uri)
        upload_url = self._upload_audio(audio_bytes)
        transcript_id = self._submit_job(upload_url, language=language, hints=hints)
        payload = self._poll_job(transcript_id)
        return _parse_assemblyai_response(payload, audio_uri, language)

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        json_body: Optional[dict] = None,
        content_type: str = "application/json",
    ) -> dict:
        headers = {
            "authorization": self.api_key,
        }
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["content-type"] = content_type
        elif body is not None and content_type:
            headers["content-type"] = content_type
        url = f"{self.base_url}{path}"
        try:
            status, raw = self._transport(
                method, url, body=body, headers=headers, timeout=self.timeout
            )
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"assemblyai_transport_unexpected: {exc!r}") from exc

        if status >= 500:
            raise STTError(f"assemblyai_5xx status={status} body={raw[:256]!r}")
        if status >= 400:
            raise STTError(f"assemblyai_4xx status={status} body={raw[:256]!r}")
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise STTError(f"assemblyai_invalid_json: {exc!r}") from exc

    def _upload_audio(self, audio_bytes: bytes) -> str:
        result = self._request(
            "POST",
            "/upload",
            body=audio_bytes,
            content_type="application/octet-stream",
        )
        upload_url = result.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url:
            raise STTError(f"assemblyai_upload_no_url: {result!r}")
        return upload_url

    def _submit_job(
        self, upload_url: str, *, language: Optional[str], hints: Sequence[str]
    ) -> str:
        body = {
            "audio_url": upload_url,
            "speaker_labels": True,
            "punctuate": True,
            "format_text": True,
        }
        if language:
            body["language_code"] = language
        if hints:
            body["word_boost"] = list(hints)
        result = self._request("POST", "/transcript", json_body=body)
        tid = result.get("id")
        if not isinstance(tid, str) or not tid:
            raise STTError(f"assemblyai_submit_no_id: {result!r}")
        return tid

    def _poll_job(self, transcript_id: str) -> dict:
        elapsed = 0.0
        while elapsed <= self.poll_max_seconds:
            payload = self._request("GET", f"/transcript/{transcript_id}")
            status = payload.get("status")
            if status == "completed":
                return payload
            if status == "error":
                raise STTError(f"assemblyai_job_error: {payload.get('error', '')!r}")
            self._sleep(self.poll_interval)
            elapsed += self.poll_interval
        raise STTError(
            f"assemblyai_poll_timeout transcript_id={transcript_id} "
            f"max={self.poll_max_seconds}"
        )


def _parse_assemblyai_response(
    payload: dict, audio_uri: str, language: Optional[str]
) -> STTResult:
    detected_language = payload.get("language_code") or language or "en"
    words_raw = payload.get("words") or []
    utterances = payload.get("utterances") or []

    turns = []
    words: list = []
    if utterances:
        for ti, utt in enumerate(utterances):
            speaker = "founder" if str(utt.get("speaker", "A")).upper() == "A" else "interviewer"
            for w in utt.get("words", ()) or ():
                words.append(
                    (
                        ti,
                        str(w.get("text", "")).strip(),
                        float(w.get("start", 0.0) or 0.0) / 1000.0,
                        float(w.get("end", 0.0) or 0.0) / 1000.0,
                        float(w.get("confidence", 0.0) or 0.0),
                    )
                )
            turns.append(
                TranscriptTurn(
                    speaker=speaker,
                    text=str(utt.get("text", "")).strip(),
                    confidence=float(utt.get("confidence", 0.0) or 0.0),
                    start_s=float(utt.get("start", 0.0) or 0.0) / 1000.0,
                    end_s=float(utt.get("end", 0.0) or 0.0) / 1000.0,
                )
            )
    else:
        for w in words_raw:
            words.append(
                (
                    0,
                    str(w.get("text", "")).strip(),
                    float(w.get("start", 0.0) or 0.0) / 1000.0,
                    float(w.get("end", 0.0) or 0.0) / 1000.0,
                    float(w.get("confidence", 0.0) or 0.0),
                )
            )
        text = payload.get("text") or ""
        if text:
            turns.append(
                TranscriptTurn(
                    speaker="founder",
                    text=str(text).strip(),
                    confidence=float(payload.get("confidence", 0.0) or 0.0),
                    start_s=float(words[0][2]) if words else 0.0,
                    end_s=float(words[-1][3]) if words else 0.0,
                )
            )

    transcript = Transcript(
        session_id=_session_id_from_uri(audio_uri),
        language=str(detected_language),
        turns=tuple(turns),
        asr_model="assemblyai:best",
    )
    avg_conf = (
        average_word_confidence(words)
        if words
        else (sum(t.confidence for t in turns) / len(turns) if turns else 0.0)
    )
    return STTResult(
        transcript=transcript,
        provenance=STTProvenance(
            stt_provider="assemblyai",
            model="best",
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
