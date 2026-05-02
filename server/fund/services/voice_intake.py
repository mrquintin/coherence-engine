"""Voice intake: phone-based founder interview orchestration (prompt 38).

Flow
----

1. ``start_call(application_id, phone_number)`` mints an
   :class:`InterviewSession` row (``channel="voice"``), asks the
   :class:`TwilioClient` to place an outbound call, and returns the
   session.
2. Twilio dials the founder, then POSTs to ``/webhooks/twilio/voice``;
   we render the first topic's TwiML there (greeting + ``<Record>``).
3. Each ``recording-completion`` webhook downloads the recording bytes,
   stores them through the object-storage adapter, and writes an
   :class:`InterviewRecording` row for the topic.
4. When all topics are recorded the ``status`` webhook emits exactly
   one ``interview_session_completed`` outbox event.

Topics are sourced from the prompt registry (``data/prompts/registry.json``,
prompt 08). The voice-intake topic ordering is deterministic — the
same registry file always produces the same TwiML script — so the
test suite can pin a snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services import interview_policy as _interview_policy
from coherence_engine.server.fund.services import object_storage as _object_storage
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.services.twilio_adapter import (
    TwilioCall,
    TwilioClient,
    TwilioConfigError,
    get_twilio_client,
)


__all__ = [
    "InterviewTopic",
    "VoiceIntakeError",
    "load_topics",
    "render_initial_twiml",
    "render_topic_twiml",
    "render_session_complete_twiml",
    "start_call",
    "start_browser_session",
    "store_recording",
    "stitch_chunks",
    "finalize_session",
    "finalize_browser_session",
    "transcribe_session_audio",
    "next_question_for_session",
]


_LOG = logging.getLogger("coherence_engine.fund.voice_intake")

_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data"
    / "prompts"
    / "registry.json"
)

# A short, deterministic, operator-readable greeting + per-topic prompt
# fragment. The user-facing copy lives here rather than in the registry
# bodies because the registry bodies are LLM system prompts (long-form);
# the spoken voice line needs a single concise sentence per topic.
_TOPIC_VOICE_LINES: Mapping[str, str] = {
    "interview_opening": (
        "Tell us about the problem you are solving and your proposed solution mechanism."
    ),
    "self_critique": (
        "Walk us through the strongest objection a sceptical investor would raise, "
        "and how you would respond to it."
    ),
}

_DEFAULT_GREETING = (
    "Hello, this is the Coherence Engine founder interview line. "
    "We will record short answers to a small number of topics. "
    "Please speak clearly after the tone."
)
_DEFAULT_FAREWELL = "Thank you. The interview is complete. Goodbye."

# Hard cap on per-topic recording length. Twilio enforces ``maxLength``
# server-side so a runaway answer cannot rack up minutes against our
# account; the value also bounds the recording-fetch I/O budget.
_MAX_TOPIC_RECORDING_SECONDS = 180


# ---------------------------------------------------------------------------
# Topic loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterviewTopic:
    """A single interview topic — id + spoken prompt line."""

    id: str
    prompt: str


class VoiceIntakeError(RuntimeError):
    """Raised when a voice-intake operation cannot proceed."""


def load_topics(registry_path: Optional[Path] = None) -> Tuple[InterviewTopic, ...]:
    """Load interview topics in deterministic registry order.

    Only registry entries with ``status == "prod"`` and a known voice
    line are surfaced; the result is a tuple so callers cannot mutate
    the loaded list and break determinism.
    """
    path = registry_path or _REGISTRY_PATH
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out: List[InterviewTopic] = []
    for entry in raw.get("prompts", []):
        if entry.get("status") != "prod":
            continue
        topic_id = str(entry.get("id", "")).strip()
        if not topic_id:
            continue
        line = _TOPIC_VOICE_LINES.get(topic_id)
        if line is None:
            continue
        out.append(InterviewTopic(id=topic_id, prompt=line))
    return tuple(out)


# ---------------------------------------------------------------------------
# TwiML rendering
# ---------------------------------------------------------------------------
#
# We emit XML by hand rather than via ``twilio.twiml.VoiceResponse`` so the
# rendering is deterministic and dependency-free. The pinned-snapshot test
# in ``test_voice_intake.py`` asserts exact bytes; if you change the
# rendering, update the snapshot intentionally.


def _twiml_say(text: str) -> str:
    return f"<Say>{_xml_escape(text)}</Say>"


def _twiml_record(*, action_url: str, topic_id: str) -> str:
    # Topic id rides on the action URL as a query param. ``<Record>`` does
    # not support arbitrary custom attributes, so the webhook handler must
    # parse ``topic_id`` from its own URL.
    sep = "&" if "?" in action_url else "?"
    full_action = f"{action_url}{sep}topic_id={topic_id}"
    safe_action = _xml_escape(full_action)
    return (
        f'<Record action="{safe_action}" '
        f'maxLength="{_MAX_TOPIC_RECORDING_SECONDS}" '
        f'recordingStatusCallback="{safe_action}" '
        f'recordingStatusCallbackMethod="POST" '
        f'finishOnKey="#" '
        f'playBeep="true" '
        f'trim="trim-silence" '
        f'recordingTrack="inbound" '
        f'recordingChannels="mono" '
        f'recordingFormat="wav" />'
    )


def render_initial_twiml(
    *,
    topics: Sequence[InterviewTopic],
    recording_action_url: str,
) -> str:
    """Render the greeting + first-topic prompt + first ``<Record>``."""
    if not topics:
        raise VoiceIntakeError("voice_intake_no_topics")
    first = topics[0]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{_twiml_say(_DEFAULT_GREETING)}"
        f"{_twiml_say(first.prompt)}"
        f"{_twiml_record(action_url=recording_action_url, topic_id=first.id)}"
        "</Response>"
    )


def render_topic_twiml(
    *,
    topic: InterviewTopic,
    recording_action_url: str,
) -> str:
    """Render the prompt + ``<Record>`` for a follow-on topic."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{_twiml_say(topic.prompt)}"
        f"{_twiml_record(action_url=recording_action_url, topic_id=topic.id)}"
        "</Response>"
    )


def render_session_complete_twiml() -> str:
    """Render the final farewell + ``<Hangup>`` after every topic recorded."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{_twiml_say(_DEFAULT_FAREWELL)}"
        "<Hangup/>"
        "</Response>"
    )


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def start_call(
    db: Session,
    *,
    application_id: str,
    phone_number: str,
    from_number: str,
    voice_webhook_url: str,
    status_callback_url: str,
    client: Optional[TwilioClient] = None,
    locale: str = "en-US",
) -> models.InterviewSession:
    """Initiate a Twilio voice call and persist the session row.

    Parameters
    ----------
    application_id
        The :class:`Application` row this call is for. Must already exist.
    phone_number
        E.164 number to dial.
    from_number
        Twilio caller-id (``TWILIO_FROM_NUMBER``).
    voice_webhook_url
        Public URL Twilio will POST to for the initial TwiML (and
        subsequent ``<Record>`` ``action`` callbacks).
    status_callback_url
        Public URL Twilio will POST call-status updates to.
    client
        Override the module-level Twilio client. Tests inject a fake.

    Returns
    -------
    models.InterviewSession
        Persisted (committed by caller) session row keyed by
        ``id`` = ``ivw_voice_<random>``.
    """
    if not phone_number:
        raise VoiceIntakeError("voice_intake_missing_phone_number")
    if not from_number:
        raise VoiceIntakeError("voice_intake_missing_from_number")

    twilio = client or get_twilio_client()
    # Initialise the adaptive-policy state so a recovery flow can read
    # ``next_question`` even before the first webhook fires.
    initial_state_json = ""
    try:
        graph = _interview_policy.load_topic_graph()
        initial_state_json = json.dumps(
            _interview_policy.init_state(graph), sort_keys=True
        )
    except _interview_policy.InterviewPolicyError:
        # Misconfigured graph fails the call setup loudly; the
        # operator must fix the JSON rather than silently running
        # the legacy linear walk.
        raise
    session = models.InterviewSession(
        id=_new_id("ivw_voice"),
        application_id=application_id,
        channel="voice",
        locale=locale,
        status="active",
        state_json=initial_state_json,
    )
    db.add(session)
    db.flush()

    try:
        call: TwilioCall = twilio.place_call(
            to=phone_number,
            from_=from_number,
            twiml_url=voice_webhook_url,
            status_callback_url=status_callback_url,
        )
    except TwilioConfigError:
        # Surface as a service-level error; the row is rolled back so a
        # misconfigured account never leaves a half-created session.
        db.rollback()
        raise
    _LOG.info(
        "voice_intake_call_placed application_id=%s session_id=%s sid=%s",
        application_id,
        session.id,
        call.sid,
    )
    return session


def _topic_object_key(application_id: str, session_id: str, topic_id: str) -> str:
    return f"interviews/{application_id}/{session_id}/{topic_id}.wav"


def store_recording(
    db: Session,
    *,
    session: models.InterviewSession,
    topic_id: str,
    recording_sid: str,
    recording_bytes: bytes,
    duration_seconds: float,
) -> models.InterviewRecording:
    """Persist a single recording: object-storage put + DB row.

    The bytes are SHA-256'd; on hash drift between expected and stored
    we raise :class:`StorageHashMismatch` (caller fails the webhook
    response, Twilio retries).
    """
    expected = _object_storage.sha256_hex(recording_bytes)
    key = _topic_object_key(session.application_id, session.id, topic_id)
    result = _object_storage.put(
        key,
        recording_bytes,
        content_type="audio/wav",
    )
    if result.sha256 != expected:
        raise _object_storage.StorageHashMismatch(
            f"interview_recording_hash_drift: expected={expected} got={result.sha256}"
        )
    from datetime import datetime, timezone

    rec = models.InterviewRecording(
        id=_new_id("rec"),
        application_id=session.application_id,
        session_id=session.id,
        topic_id=topic_id,
        recording_uri=result.uri,
        recording_sha256=result.sha256,
        duration_seconds=float(duration_seconds),
        provider_recording_sid=str(recording_sid or ""),
        status="recorded",
        completed_at=datetime.now(tz=timezone.utc),
    )
    db.add(rec)
    db.flush()
    return rec


def _topics_recorded(
    db: Session, *, session: models.InterviewSession
) -> Tuple[models.InterviewRecording, ...]:
    rows = (
        db.query(models.InterviewRecording)
        .filter(models.InterviewRecording.session_id == session.id)
        .order_by(models.InterviewRecording.started_at.asc())
        .all()
    )
    return tuple(rows)


def finalize_session(
    db: Session,
    *,
    session: models.InterviewSession,
    topics: Iterable[InterviewTopic],
    provider_call_sid: str = "",
    publisher: Optional[EventPublisher] = None,
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """Mark the session complete and emit ``interview_session_completed``.

    Idempotent: a second call after the session is already ``completed``
    returns ``None`` and does not emit a duplicate event. Returns the
    new event id when an event is emitted.
    """
    if session.status == "completed":
        return None

    expected = tuple(topics)
    recordings = _topics_recorded(db, session=session)
    by_topic = {r.topic_id: r for r in recordings}

    topic_payloads: List[dict] = []
    total_duration = 0.0
    covered = 0
    for t in expected:
        rec = by_topic.get(t.id)
        if rec is None:
            topic_payloads.append({"topic_id": t.id, "status": "skipped"})
            continue
        covered += 1
        total_duration += float(rec.duration_seconds or 0.0)
        topic_payloads.append(
            {
                "topic_id": t.id,
                "status": "recorded",
                "recording_uri": rec.recording_uri,
                "recording_sha256": rec.recording_sha256,
                "duration_seconds": float(rec.duration_seconds or 0.0),
            }
        )

    session.status = "completed"
    db.flush()

    # Record the Twilio voice-minute cost for the call (prompt 62).
    # ``total_duration`` is server-observed (sum of stored
    # ``InterviewRecording.duration_seconds``) so we never trust a
    # caller-supplied minute count. The discriminator combines the
    # session id with the provider call SID when available so a
    # webhook retry collapses to a single CostEvent row.
    if total_duration > 0 and session.application_id:
        try:
            from coherence_engine.server.fund.services.cost_pricing import (
                CostPricingError,
            )
            from coherence_engine.server.fund.services.cost_telemetry import (
                compute_idempotency_key as _cost_idem,
                record_cost as _record_cost,
            )

            twilio_sku = "twilio.voice.outbound_us"
            disc = provider_call_sid or session.id
            idem = _cost_idem(
                provider="twilio",
                sku=twilio_sku,
                application_id=session.application_id,
                discriminator=f"voice:{disc}",
            )
            _record_cost(
                db,
                provider="twilio",
                sku=twilio_sku,
                units=float(total_duration) / 60.0,
                application_id=session.application_id,
                idempotency_key=idem,
            )
        except CostPricingError as exc:  # pragma: no cover - misconfig guard
            _LOG.warning(
                "voice_intake_cost_record_skipped session_id=%s reason=%s",
                session.id,
                exc,
            )

    publisher = publisher or EventPublisher(db)
    payload: dict = {
        "application_id": session.application_id,
        "session_id": session.id,
        "channel": "voice",
        "topics": topic_payloads,
        "topics_covered": covered,
        "topics_total": len(expected) or 1,
        "duration_seconds": float(total_duration),
    }
    if provider_call_sid:
        payload["provider_call_sid"] = provider_call_sid
    result = publisher.publish(
        event_type="interview_session_completed",
        producer="voice_intake",
        trace_id=trace_id or f"voice_{session.id}",
        idempotency_key=f"voice_session_completed:{session.id}",
        payload=payload,
    )
    return result.get("event_id")


# ---------------------------------------------------------------------------
# Browser-mode (WebRTC) session lifecycle (prompt 39)
# ---------------------------------------------------------------------------
#
# Browser-mode is the in-page alternative to the Twilio voice path: a
# ``MediaRecorder`` in the founder portal emits 5-second
# ``audio/webm; codecs=opus`` chunks that are PUT directly to object
# storage via signed URLs. At session end the server stitches the
# chunks (ffmpeg concat) into a single ``full.webm`` and emits the
# same ``interview_session_completed`` outbox event the Twilio path
# emits — downstream scoring is mode-agnostic.


_BROWSER_CHUNK_CONTENT_TYPE = "audio/webm"
_BROWSER_FULL_CONTENT_TYPE = "audio/webm"
_BROWSER_DEFAULT_CHUNK_SECONDS = 5
# Hard cap on the number of chunks per session (≈ 60 minutes at 5s
# chunks). Prevents a runaway browser tab from racking up object-
# storage cost. The router rejects ``seq`` above this bound.
_BROWSER_MAX_CHUNKS = 720


def _browser_chunk_key(session_id: str, seq: int) -> str:
    return f"interviews/{session_id}/chunk_{seq:05d}.webm"


def _browser_full_key(session_id: str) -> str:
    return f"interviews/{session_id}/full.webm"


def start_browser_session(
    db: Session,
    *,
    application_id: str,
    locale: str = "en-US",
) -> models.InterviewSession:
    """Persist a new ``channel="browser"`` :class:`InterviewSession`.

    Unlike :func:`start_call`, no provider client is invoked — the
    browser captures audio locally via ``MediaRecorder`` and uploads
    chunks via signed URLs. The caller is expected to commit the
    transaction (mirrors :func:`start_call`).
    """
    if not application_id:
        raise VoiceIntakeError("voice_intake_missing_application_id")

    session = models.InterviewSession(
        id=_new_id("ivw_browser"),
        application_id=application_id,
        channel="browser",
        locale=locale,
        status="active",
    )
    db.add(session)
    db.flush()
    _LOG.info(
        "voice_intake_browser_session_started application_id=%s session_id=%s",
        application_id,
        session.id,
    )
    return session


def _next_expected_seq(db: Session, session_id: str) -> int:
    """Return the next monotonic ``seq`` the server will accept for ``session_id``.

    The server is the source of truth for sequence ordering; the
    client may *propose* a number but the router compares against the
    value returned here and rejects gaps and replays alike.
    """
    last = (
        db.query(models.InterviewChunk)
        .filter(models.InterviewChunk.session_id == session_id)
        .order_by(models.InterviewChunk.seq.desc())
        .first()
    )
    if last is None:
        return 0
    return int(last.seq) + 1


def stitch_chunks(
    db: Session,
    *,
    session: models.InterviewSession,
    ffmpeg_binary: Optional[str] = None,
) -> Tuple[str, str, int]:
    """Concatenate every recorded chunk for ``session`` into ``full.webm``.

    Returns ``(full_uri, full_sha256, total_bytes)``. Raises
    :class:`VoiceIntakeError` when no chunks are present, when ffmpeg
    is unavailable, or when the ffmpeg call exits non-zero (the
    contract is "concat-or-fail" — partial output is unacceptable).

    The chunks are downloaded from object storage in order, written
    to a temporary directory, and concatenated via ::

        ffmpeg -f concat -safe 0 -i list.txt -c copy out.webm

    Codec-copy concat is correct because every chunk is produced by
    the same ``MediaRecorder`` instance with the same opus parameters
    (see ``apps/founder_portal/src/lib/webrtc_recorder.ts``); on
    codec mismatch the caller would re-encode separately, but we do
    not do that automatically here because a codec drift indicates a
    client-side bug we want surfaced rather than papered over.
    """
    chunks = (
        db.query(models.InterviewChunk)
        .filter(
            models.InterviewChunk.session_id == session.id,
            models.InterviewChunk.status == "completed",
        )
        .order_by(models.InterviewChunk.seq.asc())
        .all()
    )
    if not chunks:
        raise VoiceIntakeError("voice_intake_no_chunks_to_stitch")

    # Verify monotonic, gap-free seq before doing any I/O.
    for expected, row in enumerate(chunks):
        if int(row.seq) != expected:
            raise VoiceIntakeError(
                f"voice_intake_chunk_seq_gap expected={expected} got={row.seq}"
            )

    binary = ffmpeg_binary or os.environ.get("FFMPEG_BINARY") or "ffmpeg"
    if shutil.which(binary) is None:
        raise VoiceIntakeError(f"voice_intake_ffmpeg_not_available binary={binary!r}")

    with tempfile.TemporaryDirectory(prefix="ivw_stitch_") as tmp:
        tmp_path = Path(tmp)
        list_path = tmp_path / "list.txt"
        chunk_paths: List[Path] = []
        with list_path.open("w", encoding="utf-8") as list_fh:
            for row in chunks:
                data = _object_storage.get(row.chunk_uri)
                # Defensive integrity check — the chunk row already
                # carries a sha256, so a drift here means tampering
                # or a partial upload that we missed at write time.
                actual = _object_storage.sha256_hex(data)
                if row.chunk_sha256 and actual != row.chunk_sha256:
                    raise _object_storage.StorageHashMismatch(
                        f"interview_chunk_hash_drift session={session.id} "
                        f"seq={row.seq} expected={row.chunk_sha256} got={actual}"
                    )
                chunk_path = tmp_path / f"chunk_{row.seq:05d}.webm"
                chunk_path.write_bytes(data)
                chunk_paths.append(chunk_path)
                # ffmpeg concat list format: ``file '<path>'`` per line.
                list_fh.write(f"file '{chunk_path.as_posix()}'\n")

        out_path = tmp_path / "full.webm"
        proc = subprocess.run(
            [
                binary,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(out_path),
            ],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise VoiceIntakeError(
                "voice_intake_ffmpeg_failed "
                f"rc={proc.returncode} stderr={proc.stderr[-512:]!r}"
            )
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise VoiceIntakeError("voice_intake_ffmpeg_empty_output")
        full_bytes = out_path.read_bytes()

    expected_hash = _object_storage.sha256_hex(full_bytes)
    full_key = _browser_full_key(session.id)
    result = _object_storage.put(
        full_key,
        full_bytes,
        content_type=_BROWSER_FULL_CONTENT_TYPE,
    )
    if result.sha256 != expected_hash:
        raise _object_storage.StorageHashMismatch(
            f"interview_full_hash_drift session={session.id} "
            f"expected={expected_hash} got={result.sha256}"
        )
    return result.uri, result.sha256, len(full_bytes)


def transcribe_session_audio(
    audio_uri: str,
    *,
    session_id: str,
    language: Optional[str] = None,
) -> Optional[dict]:
    """Run STT against ``audio_uri`` and persist the resulting transcript.

    Returns a small metadata dict suitable for embedding in the
    ``interview_session_completed`` event payload, or ``None`` when STT
    is unconfigured (no ``STT_PROVIDER_PRIMARY``) — the latter keeps
    existing test fixtures (which use synthetic non-audio bytes) and
    deployments without an STT provider working unchanged.

    Failures are surfaced as logs but do not raise: the audio is still
    in object storage and a later worker can re-attempt transcription
    out of band. The deterministic transcript-quality gate (prompt 03)
    will still flag the application as ``LOW_STT_CONFIDENCE`` when the
    eventual transcript is low-confidence.
    """
    if not os.environ.get("STT_PROVIDER_PRIMARY"):
        return None

    try:
        # Imported lazily so the STT package's optional dependencies do
        # not load when the feature is disabled.
        from coherence_engine.server.fund.services.stt import (
            STTUnavailable,
            transcribe as _stt_transcribe,
        )
        from coherence_engine.server.fund.services.transcript_quality import (
            store_transcript,
        )
    except Exception:
        _LOG.exception("voice_intake_stt_import_failed session_id=%s", session_id)
        return None

    try:
        result = _stt_transcribe(audio_uri, language=language)
    except STTUnavailable as exc:
        _LOG.warning(
            "voice_intake_stt_unavailable session_id=%s error=%r", session_id, exc
        )
        return {"status": "unavailable", "error": str(exc)}
    except Exception:
        _LOG.exception("voice_intake_stt_unexpected session_id=%s", session_id)
        return {"status": "error"}

    try:
        transcript_uri = store_transcript(result.transcript, session_id)
    except Exception:
        _LOG.exception(
            "voice_intake_transcript_store_failed session_id=%s", session_id
        )
        transcript_uri = None

    return {
        "status": "ok",
        "stt_provider": result.provenance.stt_provider,
        "model": result.provenance.model,
        "avg_confidence": float(result.provenance.avg_confidence),
        "word_count": int(result.provenance.word_count),
        "fallback_used": bool(result.provenance.fallback_used),
        "quality_flags": list(result.quality_flags),
        "transcript_uri": transcript_uri,
        "attempts": list(result.provenance.attempts),
    }


def finalize_browser_session(
    db: Session,
    *,
    session: models.InterviewSession,
    publisher: Optional[EventPublisher] = None,
    trace_id: Optional[str] = None,
    ffmpeg_binary: Optional[str] = None,
) -> Optional[Mapping[str, object]]:
    """Stitch chunks + emit ``interview_session_completed`` (idempotent).

    Returns a mapping with ``event_id``, ``full_uri``, and
    ``full_sha256`` on first call; ``None`` on a re-call against an
    already-completed session (matches :func:`finalize_session`).
    """
    if session.status == "completed":
        return None

    full_uri, full_sha256, full_size = stitch_chunks(
        db, session=session, ffmpeg_binary=ffmpeg_binary
    )

    stt_meta = transcribe_session_audio(full_uri, session_id=session.id)

    chunks = (
        db.query(models.InterviewChunk)
        .filter(models.InterviewChunk.session_id == session.id)
        .order_by(models.InterviewChunk.seq.asc())
        .all()
    )
    chunk_count = len(chunks)
    duration_seconds = float(chunk_count * _BROWSER_DEFAULT_CHUNK_SECONDS)

    session.status = "completed"
    db.flush()

    publisher = publisher or EventPublisher(db)
    payload: dict = {
        "application_id": session.application_id,
        "session_id": session.id,
        "channel": "browser",
        "topics": [
            {
                "topic_id": "browser_full",
                "status": "recorded",
                "recording_uri": full_uri,
                "recording_sha256": full_sha256,
                "duration_seconds": duration_seconds,
            }
        ],
        "topics_covered": 1,
        "topics_total": 1,
        "duration_seconds": duration_seconds,
        "chunk_count": chunk_count,
        "full_uri": full_uri,
        "full_sha256": full_sha256,
        "full_size_bytes": full_size,
    }
    if stt_meta is not None:
        payload["stt"] = stt_meta
    result = publisher.publish(
        event_type="interview_session_completed",
        producer="voice_intake",
        trace_id=trace_id or f"browser_{session.id}",
        idempotency_key=f"browser_session_completed:{session.id}",
        payload=payload,
    )
    return {
        "event_id": result.get("event_id"),
        "full_uri": full_uri,
        "full_sha256": full_sha256,
        "full_size_bytes": full_size,
        "chunk_count": chunk_count,
    }


# ---------------------------------------------------------------------------
# Adaptive-policy delegation (prompt 41)
# ---------------------------------------------------------------------------
#
# The legacy linear walk (``load_topics`` + per-topic TwiML) remains
# exposed for the existing snapshot tests and the browser intake path.
# New callers that want adaptive question selection should go through
# ``next_question_for_session`` — the function loads the policy
# state from ``InterviewSession.state_json``, asks the deterministic
# policy engine for the next question (or ``None`` to terminate),
# and writes the updated state back.


def next_question_for_session(
    db,
    session: models.InterviewSession,
    last_answer_features: Optional[_interview_policy.AnswerFeatures] = None,
    *,
    graph: Optional[_interview_policy.TopicGraph] = None,
) -> Optional[_interview_policy.Question]:
    """Delegate next-question selection to the adaptive policy engine.

    Loads ``session.state_json`` (initialising it if blank), runs the
    deterministic policy, persists the updated state, and returns
    the chosen :class:`~interview_policy.Question` (or ``None`` when
    coverage is met or the duration cap exceeded).
    """
    g = graph or _interview_policy.load_topic_graph()
    raw = session.state_json or ""
    if raw:
        state = json.loads(raw)
    else:
        state = _interview_policy.init_state(g)
    question = _interview_policy.next_question(
        state, last_answer_features, graph=g
    )
    session.state_json = json.dumps(state, sort_keys=True)
    db.flush()
    return question
