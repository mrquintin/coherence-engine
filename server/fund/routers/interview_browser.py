"""Browser-mode (WebRTC) founder interview API (prompt 39).

Three endpoints back the in-page interview surface:

* ``POST /api/v1/interviews/{session_id}/chunks:initiate``
    Mint a signed URL the browser PUTs the next 5-second chunk to.
    The router is the source of truth for ``seq`` ordering — it
    rejects gaps, replays, and out-of-order proposals from the
    client. The chunk row is written in ``status="initiated"`` and
    transitioned to ``"completed"`` only on the matching
    ``:complete`` call.

* ``POST /api/v1/interviews/{session_id}/chunks:complete``
    Records the freshly-uploaded chunk's SHA-256 + size and flips
    the row to ``"completed"``. Idempotent: re-completing a
    ``"completed"`` row returns the existing envelope.

* ``POST /api/v1/interviews/{session_id}:finalize``
    Stitches every completed chunk via ffmpeg concat, persists the
    resulting ``full.webm`` artifact, and emits exactly one
    ``interview_session_completed`` outbox event. Idempotent against
    sessions already in ``"completed"`` state.

Bytes never proxy through this router — uploads go direct to object
storage via the signed URL minted by ``:initiate``. The router only
knows about metadata.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, Depends, Header, Path, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import (
    envelope,
    error_response,
    new_request_id,
)
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.services import object_storage, voice_intake


router = APIRouter(prefix="/interviews", tags=["interviews"])

_LOG = logging.getLogger("coherence_engine.fund.interview_browser")


_CHUNK_URL_EXPIRES_SECONDS = 300
_CHUNK_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB per chunk (≈5s of opus + slack)
_CHUNK_CONTENT_TYPE = "audio/webm"
# Mirrors :data:`voice_intake._BROWSER_MAX_CHUNKS`. Kept private to
# the service module; we duplicate the bound here so the router can
# 4xx without importing a private name.
_MAX_CHUNKS_PER_SESSION = 720


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _chunk_object_key(session_id: str, seq: int) -> str:
    return f"interviews/{session_id}/chunk_{seq:05d}.webm"


def _mint_signed_chunk_url(
    backend, key: str, expires_in: int
) -> Tuple[str, Dict[str, str]]:
    """Mirror of ``applications._mint_signed_upload_url`` for chunk PUTs.

    Local backend returns a synthetic URL (the test harness intercepts
    it); S3 / Supabase backends return real provider-signed URLs. The
    helper is duplicated here rather than imported to keep router
    boundaries clean — applications.py owns deck/supporting upload
    behavior and that signature should not creep across surfaces.
    """
    name = getattr(backend, "backend_name", "")
    if name == "s3":
        client = backend._client_for()  # type: ignore[attr-defined]
        url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": backend.bucket,
                "Key": key,
                "ContentType": _CHUNK_CONTENT_TYPE,
            },
            ExpiresIn=expires_in,
        )
        return url, {"Content-Type": _CHUNK_CONTENT_TYPE}
    if name == "supabase":
        upload_url = backend._object_url(key)  # type: ignore[attr-defined]
        return upload_url, {
            "Content-Type": _CHUNK_CONTENT_TYPE,
            "x-upsert": "true",
            "Authorization": f"Bearer {backend._service_role_key or ''}",  # type: ignore[attr-defined]
        }
    bucket = getattr(backend, "bucket", "default")
    return f"local://upload/{bucket}/{key}", {"Content-Type": _CHUNK_CONTENT_TYPE}


def _load_session(
    db: Session, session_id: str
) -> Optional[models.InterviewSession]:
    return (
        db.query(models.InterviewSession)
        .filter(models.InterviewSession.id == session_id)
        .one_or_none()
    )


@router.post("/{session_id}/chunks:initiate", status_code=201)
def initiate_chunk(
    session_id: str = Path(...),
    request: Request = None,
    body: Dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Mint a signed URL for the next chunk and write the staging row.

    Body: ``{ "seq": <int>, "size_bytes": <int> }``. The server
    validates ``seq`` against the next-expected-seq for the session
    and refuses gaps, replays, or numbers above the per-session cap.
    """
    request_id = x_request_id or new_request_id()
    session = _load_session(db, session_id)
    if session is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "interview session not found"
        )
    if session.channel != "browser":
        return error_response(
            request_id,
            409,
            "WRONG_CHANNEL",
            f"session channel is {session.channel!r}, not 'browser'",
        )
    if session.status != "active":
        return error_response(
            request_id,
            409,
            "SESSION_NOT_ACTIVE",
            f"session status is {session.status!r}, expected 'active'",
        )

    try:
        proposed_seq = int(body.get("seq"))
    except (TypeError, ValueError):
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "seq must be an integer",
        )
    if proposed_seq < 0 or proposed_seq >= _MAX_CHUNKS_PER_SESSION:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            f"seq out of range [0, {_MAX_CHUNKS_PER_SESSION})",
        )
    try:
        claimed_size = int(body.get("size_bytes") or 0)
    except (TypeError, ValueError):
        claimed_size = -1
    if claimed_size <= 0 or claimed_size > _CHUNK_MAX_BYTES:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            f"size_bytes must be in (0, {_CHUNK_MAX_BYTES}]",
        )

    # Server is the authority on ordering. The client must propose
    # the same seq the server expects; otherwise reject so a buggy
    # retry loop cannot quietly skip a chunk.
    expected_seq = voice_intake._next_expected_seq(db, session.id)
    if proposed_seq != expected_seq:
        return error_response(
            request_id,
            409,
            "SEQ_OUT_OF_ORDER",
            f"expected seq={expected_seq}, got seq={proposed_seq}",
        )

    backend = object_storage.get_object_storage()
    key = _chunk_object_key(session.id, proposed_seq)
    uri = object_storage.format_uri(
        backend.backend_name, getattr(backend, "bucket", "default"), key
    )
    upload_url, headers = _mint_signed_chunk_url(
        backend, key, _CHUNK_URL_EXPIRES_SECONDS
    )
    expires_at = _utc_now() + timedelta(seconds=_CHUNK_URL_EXPIRES_SECONDS)

    chunk = models.InterviewChunk(
        id=f"chk_{uuid.uuid4().hex[:24]}",
        session_id=session.id,
        application_id=session.application_id,
        seq=proposed_seq,
        chunk_uri=uri,
        chunk_sha256="",
        size_bytes=0,
        content_type=_CHUNK_CONTENT_TYPE,
        status="initiated",
    )
    db.add(chunk)
    db.commit()

    return envelope(
        request_id=request_id,
        data={
            "chunk_id": chunk.id,
            "session_id": session.id,
            "seq": proposed_seq,
            "upload_url": upload_url,
            "headers": headers,
            "expires_at": expires_at.isoformat(),
            "key": key,
            "uri": uri,
            "max_bytes": _CHUNK_MAX_BYTES,
        },
    )


@router.post("/{session_id}/chunks:complete")
def complete_chunk(
    session_id: str = Path(...),
    request: Request = None,
    body: Dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Record the chunk's verified size + SHA-256 and mark it completed."""
    request_id = x_request_id or new_request_id()
    session = _load_session(db, session_id)
    if session is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "interview session not found"
        )

    chunk_id = str(body.get("chunk_id") or "").strip()
    if not chunk_id:
        return error_response(
            request_id, 422, "VALIDATION_ERROR", "chunk_id is required"
        )

    chunk = (
        db.query(models.InterviewChunk)
        .filter(
            models.InterviewChunk.id == chunk_id,
            models.InterviewChunk.session_id == session.id,
        )
        .one_or_none()
    )
    if chunk is None:
        return error_response(request_id, 404, "NOT_FOUND", "chunk not found")
    if chunk.status == "completed":
        return envelope(
            request_id=request_id,
            data={
                "chunk_id": chunk.id,
                "session_id": session.id,
                "seq": chunk.seq,
                "uri": chunk.chunk_uri,
                "size_bytes": chunk.size_bytes,
                "sha256": chunk.chunk_sha256,
                "status": "completed",
            },
        )

    backend = object_storage.get_object_storage()
    try:
        data = backend.get(chunk.chunk_uri)
    except object_storage.StorageNotFound:
        return error_response(
            request_id,
            409,
            "CHUNK_NOT_FOUND",
            "chunk not found at signed URL — re-upload required",
        )

    size_bytes = len(data)
    if size_bytes <= 0 or size_bytes > _CHUNK_MAX_BYTES:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            f"chunk size out of range: got={size_bytes}",
        )
    sha = object_storage.sha256_hex(data)
    chunk.chunk_sha256 = sha
    chunk.size_bytes = size_bytes
    chunk.status = "completed"
    chunk.completed_at = _utc_now()
    db.commit()

    return envelope(
        request_id=request_id,
        data={
            "chunk_id": chunk.id,
            "session_id": session.id,
            "seq": chunk.seq,
            "uri": chunk.chunk_uri,
            "size_bytes": size_bytes,
            "sha256": sha,
            "status": "completed",
        },
    )


@router.post("/{session_id}:finalize")
def finalize_browser(
    session_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Stitch chunks + emit ``interview_session_completed``.

    Idempotent: a second call against a ``completed`` session returns
    a 200 envelope with the previously-stitched URI.
    """
    request_id = x_request_id or new_request_id()
    session = _load_session(db, session_id)
    if session is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "interview session not found"
        )
    if session.channel != "browser":
        return error_response(
            request_id,
            409,
            "WRONG_CHANNEL",
            f"session channel is {session.channel!r}, not 'browser'",
        )

    if session.status == "completed":
        # Surface the already-stitched URI from the most recent chunk
        # row's session full_uri convention.
        full_key = f"interviews/{session.id}/full.webm"
        backend = object_storage.get_object_storage()
        full_uri = object_storage.format_uri(
            backend.backend_name, getattr(backend, "bucket", "default"), full_key
        )
        return envelope(
            request_id=request_id,
            data={
                "session_id": session.id,
                "status": "completed",
                "full_uri": full_uri,
                "idempotent": True,
            },
        )

    try:
        result = voice_intake.finalize_browser_session(db, session=session)
    except voice_intake.VoiceIntakeError as exc:
        db.rollback()
        return error_response(
            request_id, 422, "FINALIZE_FAILED", f"finalize failed: {exc}"
        )
    except object_storage.StorageHashMismatch as exc:
        db.rollback()
        return error_response(
            request_id, 500, "STORAGE_HASH_DRIFT", str(exc)
        )

    db.commit()
    return envelope(
        request_id=request_id,
        data={
            "session_id": session.id,
            "status": "completed",
            "event_id": (result or {}).get("event_id"),
            "full_uri": (result or {}).get("full_uri"),
            "full_sha256": (result or {}).get("full_sha256"),
            "chunk_count": (result or {}).get("chunk_count"),
            "idempotent": False,
        },
    )
