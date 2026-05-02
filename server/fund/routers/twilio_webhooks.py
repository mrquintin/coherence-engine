"""Twilio Voice webhook routes (prompt 38).

Three endpoints:

* ``POST /webhooks/twilio/voice``  — initial TwiML render (greeting +
  first topic prompt + first ``<Record>``). Twilio POSTs the call's
  metadata; the response body is XML.
* ``POST /webhooks/twilio/recording`` — recording-status callback.
  Twilio POSTs ``RecordingSid``, ``RecordingUrl``, ``RecordingDuration``,
  and the ``topic_id`` query param we embedded in the ``<Record>``
  ``action`` URL. The handler authenticates the recording fetch, puts
  the bytes to object storage, and writes an ``InterviewRecording``
  row. Response body is the next topic's TwiML (or a final farewell
  + ``<Hangup>`` once every topic is recorded).
* ``POST /webhooks/twilio/status`` — call-status updates (``initiated``,
  ``ringing``, ``in-progress``, ``completed``, ``failed``). When the
  call reaches a terminal state we emit
  ``interview_session_completed`` exactly once (idempotent).

Signature verification
----------------------

Every route enforces a Twilio signature check via
:class:`RequestValidator` against ``TWILIO_AUTH_TOKEN``. The check is
mandatory in ``staging`` and ``prod`` and can only be skipped in
``dev`` when the operator explicitly sets
``TWILIO_VALIDATE_WEBHOOK_SIGNATURE=false``. A failed check returns
``401 UNAUTHORIZED`` and never mutates state.

The full URL Twilio actually signed is reconstructed from the
``X-Forwarded-Proto`` / ``X-Forwarded-Host`` headers when present, since
Twilio computes its signature against the *public* URL — not the
internal URL FastAPI sees behind a proxy.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import error_response, new_request_id
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.services import voice_intake as _voice_intake
from coherence_engine.server.fund.services.env_gates import is_dev
from coherence_engine.server.fund.services.twilio_adapter import (
    RequestValidator,  # noqa: F401  (re-exported for callers / tests)
    TwilioClient,
    TwilioConfigError,
    get_twilio_client,
    verify_twilio_signature,
)


router = APIRouter(tags=["twilio_webhooks"])

LOGGER = logging.getLogger("coherence_engine.fund.twilio_webhooks")


_XML_MEDIA_TYPE = "application/xml"


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _auth_token() -> str:
    return os.environ.get("TWILIO_AUTH_TOKEN", "")


def _signature_validation_required() -> bool:
    """Whether to enforce the Twilio signature check on incoming webhooks.

    Default is ``True``. In ``dev`` only, the operator can opt out by
    setting ``TWILIO_VALIDATE_WEBHOOK_SIGNATURE=false`` (e.g. when
    testing against a tunnel without a real Twilio account). Staging
    and prod ignore the env var and always enforce.
    """
    raw = os.environ.get("TWILIO_VALIDATE_WEBHOOK_SIGNATURE", "true").strip().lower()
    operator_disabled = raw in {"0", "false", "no", "off"}
    if operator_disabled and is_dev():
        return False
    return True


def _public_url(request: Request) -> str:
    """Reconstruct the public URL Twilio used to sign the request.

    Twilio computes the signature against the URL the *founder phone*
    reached — i.e. the public URL configured on the Twilio number, not
    the internal URL FastAPI sees after a load balancer / tunnel. We
    honor ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` when present and
    fall back to ``request.url`` otherwise.
    """
    headers = {k.lower(): v for k, v in request.headers.items()}
    forwarded_host = headers.get("x-forwarded-host") or headers.get("host")
    forwarded_proto = headers.get("x-forwarded-proto")
    if forwarded_host and forwarded_proto:
        return f"{forwarded_proto}://{forwarded_host}{request.url.path}{('?' + request.url.query) if request.url.query else ''}"
    return str(request.url)


async def _form_params(request: Request) -> Dict[str, str]:
    form = await request.form()
    out: Dict[str, str] = {}
    for k, v in form.multi_items():
        out[str(k)] = str(v)
    return out


def _verify_or_error(
    request: Request,
    request_id: str,
    params: Dict[str, str],
) -> Optional[Response]:
    """Run signature verification; return an error Response on failure."""
    if not _signature_validation_required():
        return None
    headers = {k.lower(): v for k, v in request.headers.items()}
    signature = headers.get("x-twilio-signature", "")
    ok = verify_twilio_signature(
        auth_token=_auth_token(),
        url=_public_url(request),
        params=params,
        signature_header=signature,
    )
    if not ok:
        LOGGER.warning(
            "twilio_webhook_signature_invalid path=%s request_id=%s",
            request.url.path,
            request_id,
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED", "invalid twilio signature"
        )
    return None


def _xml_response(body: str, status_code: int = 200) -> Response:
    return Response(content=body, status_code=status_code, media_type=_XML_MEDIA_TYPE)


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------


_TEST_RECORDING_FETCHER = None


def set_recording_fetcher_for_tests(fn) -> None:
    """Override the recording-bytes fetcher (test-only seam).

    Production resolves the bytes via :func:`get_twilio_client` →
    ``fetch_recording``; tests substitute a deterministic fake to keep
    the suite hermetic (no Twilio API calls).
    """
    global _TEST_RECORDING_FETCHER
    _TEST_RECORDING_FETCHER = fn


def reset_recording_fetcher_for_tests() -> None:
    global _TEST_RECORDING_FETCHER
    _TEST_RECORDING_FETCHER = None


def _fetch_recording_bytes(recording_sid: str, recording_url: str) -> bytes:
    if _TEST_RECORDING_FETCHER is not None:
        return _TEST_RECORDING_FETCHER(recording_sid, recording_url)
    client: TwilioClient = get_twilio_client()
    return client.fetch_recording(recording_sid)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _session_by_id(db: Session, session_id: str) -> Optional[models.InterviewSession]:
    """Resolve the InterviewSession row from a webhook ``session_id`` param.

    The session id is embedded as a query parameter on the URLs we hand
    Twilio at :func:`voice_intake.start_call` time (Twilio does not
    expose its own ``CallSid`` until *after* the call is placed, so the
    correlation has to ride on the URL). Twilio preserves these query
    parameters on each callback to that URL.
    """
    if not session_id:
        return None
    return (
        db.query(models.InterviewSession)
        .filter(models.InterviewSession.id == session_id)
        .one_or_none()
    )


def _session_id_from_request(request: Request, params: Dict[str, str]) -> str:
    return (
        request.query_params.get("session_id")
        or params.get("session_id")
        or ""
    ).strip()


def _next_topic(
    topics: Tuple[_voice_intake.InterviewTopic, ...],
    recorded_topic_ids: set,
) -> Optional[_voice_intake.InterviewTopic]:
    for t in topics:
        if t.id not in recorded_topic_ids:
            return t
    return None


# ---------------------------------------------------------------------------
# Route: initial voice TwiML
# ---------------------------------------------------------------------------


@router.post("/webhooks/twilio/voice")
async def twilio_voice_initial(
    request: Request,
    db: Session = Depends(get_db),
):
    """Render the greeting + first topic prompt + first ``<Record>``.

    Twilio POSTs ``CallSid``, ``From``, ``To``, ``CallStatus``. We use
    ``CallSid`` to resolve the session row (or — when the operator
    minted the session with a known id and seeded the call-sid via the
    ``session_id`` form field — fall back to that).
    """
    request_id = new_request_id()
    params = await _form_params(request)
    err = _verify_or_error(request, request_id, params)
    if err is not None:
        return err

    session_id = _session_id_from_request(request, params)
    session = _session_by_id(db, session_id)
    if session is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "interview session not found"
        )

    topics = _voice_intake.load_topics()
    if not topics:
        return error_response(
            request_id, 503, "VOICE_INTAKE_NO_TOPICS", "no interview topics configured"
        )

    base = _public_url(request).split("?", 1)[0]
    recording_action_url = (
        base.rsplit("/", 1)[0] + f"/recording?session_id={session.id}"
    )
    body = _voice_intake.render_initial_twiml(
        topics=topics, recording_action_url=recording_action_url
    )
    return _xml_response(body)


# ---------------------------------------------------------------------------
# Route: recording-completion callback
# ---------------------------------------------------------------------------


@router.post("/webhooks/twilio/recording")
async def twilio_recording_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    request_id = new_request_id()
    params = await _form_params(request)
    err = _verify_or_error(request, request_id, params)
    if err is not None:
        return err

    topic_id = (
        request.query_params.get("topic_id")
        or params.get("topic_id")
        or ""
    ).strip()
    recording_sid = params.get("RecordingSid", "")
    recording_url = params.get("RecordingUrl", "")
    duration = float(params.get("RecordingDuration", "0") or "0")

    if not topic_id:
        return error_response(
            request_id, 400, "VALIDATION_ERROR", "topic_id missing on recording callback"
        )

    session_id = _session_id_from_request(request, params)
    session = _session_by_id(db, session_id)
    if session is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "interview session not found"
        )

    # Idempotency: a duplicate recording webhook (Twilio retries on 5xx)
    # should not double-write. Look up by (session_id, topic_id).
    existing = (
        db.query(models.InterviewRecording)
        .filter(models.InterviewRecording.session_id == session.id)
        .filter(models.InterviewRecording.topic_id == topic_id)
        .one_or_none()
    )
    if existing is None:
        try:
            recording_bytes = _fetch_recording_bytes(recording_sid, recording_url)
        except TwilioConfigError as exc:
            return error_response(
                request_id, 503, "PROVIDER_UNAVAILABLE", str(exc)
            )
        _voice_intake.store_recording(
            db,
            session=session,
            topic_id=topic_id,
            recording_sid=recording_sid,
            recording_bytes=recording_bytes,
            duration_seconds=duration,
        )
        db.commit()

    # Decide what TwiML to return next: prompt the next topic, or
    # farewell + hangup if every topic is recorded.
    topics = _voice_intake.load_topics()
    recorded_ids = {
        r.topic_id
        for r in (
            db.query(models.InterviewRecording)
            .filter(models.InterviewRecording.session_id == session.id)
            .all()
        )
    }
    nxt = _next_topic(topics, recorded_ids)
    base = _public_url(request).split("?", 1)[0]
    if nxt is None:
        body = _voice_intake.render_session_complete_twiml()
    else:
        body = _voice_intake.render_topic_twiml(
            topic=nxt,
            recording_action_url=f"{base}?session_id={session.id}",
        )
    return _xml_response(body)


# ---------------------------------------------------------------------------
# Route: call-status update
# ---------------------------------------------------------------------------


_TERMINAL_CALL_STATUSES = {"completed", "failed", "no-answer", "canceled", "busy"}


@router.post("/webhooks/twilio/status")
async def twilio_status_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    request_id = new_request_id()
    params = await _form_params(request)
    err = _verify_or_error(request, request_id, params)
    if err is not None:
        return err

    call_sid = params.get("CallSid", "")
    call_status = (params.get("CallStatus", "") or "").strip().lower()
    session_id = _session_id_from_request(request, params)
    session = _session_by_id(db, session_id)
    if session is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "interview session not found"
        )

    if call_status not in _TERMINAL_CALL_STATUSES:
        # Non-terminal updates (initiated, ringing, in-progress) are
        # informational; we just 200 without mutating session state.
        return _xml_response(
            '<?xml version="1.0" encoding="UTF-8"?><Response/>'
        )

    topics = _voice_intake.load_topics()
    event_id = _voice_intake.finalize_session(
        db,
        session=session,
        topics=topics,
        provider_call_sid=call_sid,
    )
    db.commit()
    LOGGER.info(
        "twilio_call_status_terminal call_sid=%s status=%s event_id=%s",
        call_sid,
        call_status,
        event_id,
    )
    return _xml_response('<?xml version="1.0" encoding="UTF-8"?><Response/>')


__all__: Tuple[str, ...] = (
    "router",
    "set_recording_fetcher_for_tests",
    "reset_recording_fetcher_for_tests",
)


# Silence linter: ``Any`` import is intentional for future-proofing
_ = Any
