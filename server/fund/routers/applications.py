"""Application routers for fund workflow."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Body, Depends, Header, Path, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import envelope, error_response, new_request_id
from coherence_engine.server.fund.database import get_db
from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository
from coherence_engine.server.fund.schemas.api import (
    CreateApplicationRequest,
    CreateEscalationPacketRequest,
    CreateInterviewSessionRequest,
    TriggerScoringRequest,
)
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services.application_service import ApplicationService
from coherence_engine.server.fund.services.decision_artifact import ARTIFACT_KIND
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.security import audit_log, enforce_roles
from coherence_engine.server.fund.security.auth import current_founder

router = APIRouter(prefix="/applications", tags=["applications"])


# ---------------------------------------------------------------------------
# Upload helpers (signed URL minting + persistence via IdempotencyRecord)
# ---------------------------------------------------------------------------

UPLOAD_URL_EXPIRES_SECONDS = 600
UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB
ALLOWED_UPLOAD_KINDS = {"deck", "supporting"}
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "image/png",
    "image/jpeg",
    "text/plain",
}
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\- ]{1,255}$")


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _build_upload_key(application_id: str, kind: str, filename: str) -> str:
    safe_name = filename.replace("/", "_").replace("..", "_")
    token = uuid.uuid4().hex[:12]
    return f"applications/{application_id}/{kind}/{token}-{safe_name}"


def _mint_signed_upload_url(
    backend, key: str, content_type: str, expires_in: int
) -> Tuple[str, Dict[str, str]]:
    """Return ``(upload_url, request_headers)`` for a direct PUT upload.

    Dispatches on backend type rather than mutating the storage backend
    interface. Local backend returns a synthetic in-process URL because
    presigning a filesystem path is not meaningful — production code uses
    S3 or Supabase. Tests inject their own backend via
    :func:`object_storage.set_object_storage`.
    """
    name = getattr(backend, "backend_name", "")
    if name == "s3":
        client = backend._client_for()  # type: ignore[attr-defined]
        url = client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": backend.bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
        )
        return url, {"Content-Type": content_type}
    if name == "supabase":
        # Supabase Storage uses POST with x-upsert + bearer token. The browser
        # fetches with these headers directly against /storage/v1/object/<bucket>/<key>.
        upload_url = backend._object_url(key)  # type: ignore[attr-defined]
        return upload_url, {
            "Content-Type": content_type,
            "x-upsert": "true",
            "Authorization": f"Bearer {backend._service_role_key or ''}",  # type: ignore[attr-defined]
        }
    # Local / in-memory: synthetic URL the test harness can intercept.
    bucket = getattr(backend, "bucket", "default")
    return f"local://upload/{bucket}/{key}", {"Content-Type": content_type}


def _save_upload_record(
    repo: ApplicationRepository,
    application_id: str,
    upload_id: str,
    record: Dict[str, Any],
) -> None:
    """Persist upload state in the IdempotencyRecord table.

    Reusing :class:`IdempotencyRecord` keeps the change scoped to this
    router (no schema migration). The ``endpoint`` value is namespaced
    so it cannot collide with real idempotency rows.
    """
    endpoint = f"upload:{application_id}"
    repo.save_idempotency_response(endpoint, upload_id, record)


def _load_upload_record(
    repo: ApplicationRepository, application_id: str, upload_id: str
) -> Optional[Dict[str, Any]]:
    endpoint = f"upload:{application_id}"
    return repo.get_idempotency_response(endpoint, upload_id)


def _founder_owns_application(founder, application) -> bool:
    """Defense-in-depth ownership check.

    RLS scopes Postgres rows to the founder, but the API-key path
    bypasses RLS. This function makes the ownership check explicit at
    the application layer so a service-role bug cannot expose another
    founder's data via the founder-JWT path.
    """
    if founder is None:
        return True  # service-role caller; ownership enforced elsewhere
    if application is None:
        return False
    return str(application.founder_id) == str(founder.id)


def _require_idempotency(idempotency_key: Optional[str], request_id: str):
    if not idempotency_key or not idempotency_key.strip():
        return None, error_response(
            request_id=request_id,
            status_code=400,
            code="VALIDATION_ERROR",
            message="Idempotency-Key header is required",
            details=[{"field": "Idempotency-Key", "issue": "missing"}],
        )
    return idempotency_key.strip(), None


@router.post("", status_code=201)
def create_application(
    req: CreateApplicationRequest,
    request: Request,
    db: Session = Depends(get_db),
    founder=Depends(current_founder),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    if founder is None:
        # Service-role path retains the existing role check.
        denied = enforce_roles(request, ("analyst", "admin"))
        if denied:
            return denied
    request_id = x_request_id or new_request_id()
    idem, err = _require_idempotency(idempotency_key, request_id)
    if err:
        return err

    endpoint = "POST:/applications"
    repo = ApplicationRepository(db)
    cached = repo.get_idempotency_response(endpoint, idem)
    if cached:
        return cached

    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    if not (payload["consent"]["ai_assessment"] and payload["consent"]["recording"] and payload["consent"]["data_processing"]):
        return error_response(
            request_id=request_id,
            status_code=400,
            code="VALIDATION_ERROR",
            message="All consent flags must be true",
            details=[{"field": "consent", "issue": "all consent values must be true"}],
        )

    service = ApplicationService(repo, EventPublisher(db))
    ids = service.create_application(payload)
    if founder is not None:
        # Re-link the application to the JWT-authenticated founder so the
        # service can't be tricked into associating an application with a
        # founder identity the caller didn't authenticate as.
        app_row = repo.get_application(ids["application_id"])
        if app_row is not None:
            app_row.founder_id = founder.id
            ids["founder_id"] = founder.id
            db.flush()
    response_payload = envelope(
        request_id=request_id,
        data={"application_id": ids["application_id"], "founder_id": ids["founder_id"], "status": "intake_created"},
    )
    repo.save_idempotency_response(endpoint, idem, response_payload)
    db.commit()
    audit_log("application_create", request, "allowed", {"application_id": ids["application_id"]})
    return response_payload


@router.get("/{application_id}")
def get_application(
    application_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    founder=Depends(current_founder),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    if founder is None:
        denied = enforce_roles(request, ("viewer", "analyst", "admin"))
        if denied:
            return denied
    request_id = x_request_id or new_request_id()
    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")
    if not _founder_owns_application(founder, app):
        return error_response(request_id, 403, "FORBIDDEN", "application not owned by caller")
    return envelope(
        request_id=request_id,
        data={
            "application_id": app.id,
            "founder_id": app.founder_id,
            "status": app.status,
            "one_liner": app.one_liner,
            "preferred_channel": app.preferred_channel,
            "scoring_mode": app.scoring_mode,
            "created_at": app.created_at.isoformat() if app.created_at else "",
        },
    )


@router.post("/{application_id}/uploads:initiate", status_code=201)
def initiate_upload(
    request: Request,
    application_id: str = Path(...),
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    founder=Depends(current_founder),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Mint a direct-PUT signed URL for a deck or supporting document.

    Body: ``{ filename, content_type, size_bytes, kind }``.
    Returns: ``{ upload_id, upload_url, headers, expires_at, key, uri }``.

    The client uploads bytes directly to ``upload_url`` (signed by the
    storage backend), then calls ``:complete`` to finalize. The router
    never proxies file bytes — it only mints the URL, stamps an expiry,
    and records the intended URI on the application.
    """
    if request is None:
        raise RuntimeError("request_context_missing")
    if founder is None:
        denied = enforce_roles(request, ("analyst", "admin"))
        if denied:
            return denied
    request_id = x_request_id or new_request_id()

    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")
    if not _founder_owns_application(founder, app):
        return error_response(
            request_id, 403, "FORBIDDEN", "application not owned by caller"
        )

    filename = str(body.get("filename") or "").strip()
    content_type = str(body.get("content_type") or "").strip().lower()
    kind = str(body.get("kind") or "deck").strip().lower()
    try:
        size_bytes = int(body.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = -1

    if not _SAFE_FILENAME_RE.match(filename):
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "filename invalid",
            details=[{"field": "filename", "issue": "must match [A-Za-z0-9._- ]{1,255}"}],
        )
    if content_type not in ALLOWED_CONTENT_TYPES:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "content_type not allowed",
            details=[{"field": "content_type", "issue": f"got {content_type!r}"}],
        )
    if kind not in ALLOWED_UPLOAD_KINDS:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "kind must be 'deck' or 'supporting'",
        )
    if size_bytes <= 0 or size_bytes > UPLOAD_MAX_BYTES:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "size_bytes out of range",
            details=[
                {
                    "field": "size_bytes",
                    "issue": f"must be 1..{UPLOAD_MAX_BYTES}",
                }
            ],
        )

    backend = object_storage.get_object_storage()
    key = _build_upload_key(application_id, kind, filename)
    uri = object_storage.format_uri(
        backend.backend_name, getattr(backend, "bucket", "default"), key
    )
    upload_url, headers = _mint_signed_upload_url(
        backend, key, content_type, UPLOAD_URL_EXPIRES_SECONDS
    )
    expires_at = _utc_now() + timedelta(seconds=UPLOAD_URL_EXPIRES_SECONDS)
    upload_id = f"upl_{uuid.uuid4().hex[:16]}"

    record = {
        "upload_id": upload_id,
        "application_id": application_id,
        "kind": kind,
        "key": key,
        "uri": uri,
        "filename": filename,
        "content_type": content_type,
        "claimed_size_bytes": size_bytes,
        "expires_at": expires_at.isoformat(),
        "status": "initiated",
    }
    _save_upload_record(repo, application_id, upload_id, record)

    # Record the intended URI on the application so a partial upload still
    # leaves a forensic trail. ``transcript_uri`` is the closest existing
    # column for the deck/supporting upload until a dedicated column ships.
    if kind == "deck":
        app.transcript_uri = uri
        app.updated_at = _utc_now()
        db.flush()
    db.commit()

    audit_log(
        "upload_initiate",
        request,
        "allowed",
        {"application_id": application_id, "upload_id": upload_id, "kind": kind},
    )
    return envelope(
        request_id=request_id,
        data={
            "upload_id": upload_id,
            "upload_url": upload_url,
            "headers": headers,
            "expires_at": expires_at.isoformat(),
            "key": key,
            "uri": uri,
            "max_bytes": UPLOAD_MAX_BYTES,
        },
    )


@router.post("/{application_id}/uploads:complete")
def complete_upload(
    request: Request,
    application_id: str = Path(...),
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    founder=Depends(current_founder),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Finalize a previously-initiated upload after the client PUT.

    Verifies the object exists in the storage backend and that the
    server-observed size matches the storage backend (NOT the
    client-supplied ``size_bytes`` from ``:initiate``). Idempotent:
    re-calling with the same ``upload_id`` returns the same envelope.
    """
    if request is None:
        raise RuntimeError("request_context_missing")
    if founder is None:
        denied = enforce_roles(request, ("analyst", "admin"))
        if denied:
            return denied
    request_id = x_request_id or new_request_id()
    upload_id = str(body.get("upload_id") or "").strip()
    if not upload_id:
        return error_response(
            request_id, 422, "VALIDATION_ERROR", "upload_id is required"
        )

    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")
    if not _founder_owns_application(founder, app):
        return error_response(
            request_id, 403, "FORBIDDEN", "application not owned by caller"
        )

    record = _load_upload_record(repo, application_id, upload_id)
    if not record:
        return error_response(request_id, 404, "NOT_FOUND", "upload not found")

    # Idempotent short-circuit: if already completed, return the same envelope.
    if record.get("status") == "completed":
        return envelope(
            request_id=request_id,
            data={
                "upload_id": upload_id,
                "uri": record["uri"],
                "size_bytes": record.get("verified_size_bytes"),
                "status": "completed",
            },
        )

    # Verify the object actually exists in storage and inspect its size.
    backend = object_storage.get_object_storage()
    try:
        data = backend.get(record["uri"])
    except object_storage.StorageNotFound:
        return error_response(
            request_id,
            409,
            "UPLOAD_NOT_FOUND",
            "object not found at signed URL — re-upload required",
        )
    except Exception as exc:  # pragma: no cover - storage transport errors
        return error_response(
            request_id, 500, "STORAGE_ERROR", f"storage read failed: {exc}"
        )

    verified_size = len(data)
    if verified_size <= 0 or verified_size > UPLOAD_MAX_BYTES:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "verified size out of range",
            details=[
                {"field": "size_bytes", "issue": f"verified={verified_size}"}
            ],
        )

    record["status"] = "completed"
    record["verified_size_bytes"] = verified_size
    record["completed_at"] = _utc_now().isoformat()
    # Overwrite the prior idempotency row with the updated record.
    db.query(models.IdempotencyRecord).filter(
        models.IdempotencyRecord.endpoint == f"upload:{application_id}",
        models.IdempotencyRecord.idempotency_key == upload_id,
    ).delete()
    db.flush()
    _save_upload_record(repo, application_id, upload_id, record)
    db.commit()

    audit_log(
        "upload_complete",
        request,
        "allowed",
        {
            "application_id": application_id,
            "upload_id": upload_id,
            "size_bytes": verified_size,
        },
    )
    return envelope(
        request_id=request_id,
        data={
            "upload_id": upload_id,
            "uri": record["uri"],
            "size_bytes": verified_size,
            "status": "completed",
        },
    )


@router.post("/{application_id}/interview-sessions", status_code=201)
def create_interview_session(
    req: CreateInterviewSessionRequest,
    request: Request,
    application_id: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    denied = enforce_roles(request, ("analyst", "admin"))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    idem, err = _require_idempotency(idempotency_key, request_id)
    if err:
        return err
    endpoint = f"POST:/applications/{application_id}/interview-sessions"
    repo = ApplicationRepository(db)
    cached = repo.get_idempotency_response(endpoint, idem)
    if cached:
        return cached

    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")

    service = ApplicationService(repo, EventPublisher(db))
    result = service.create_interview_session(application_id, req.channel.value, req.locale)
    routing = {
        "phone_number": "+15551234567" if req.channel.value == "phone" else None,
        "webrtc_room_url": f"https://voice.local/room/{result['interview_id']}" if req.channel.value != "phone" else None,
    }
    response_payload = envelope(
        request_id=request_id,
        data={"interview_id": result["interview_id"], "session_token": f"tok_{result['interview_id'][-12:]}", "routing": routing},
    )
    repo.save_idempotency_response(endpoint, idem, response_payload)
    db.commit()
    audit_log("interview_session_create", request, "allowed", {"application_id": application_id})
    return response_payload


@router.post("/{application_id}/score", status_code=202)
def trigger_scoring(
    req: TriggerScoringRequest,
    request: Request,
    application_id: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    denied = enforce_roles(request, ("analyst", "admin"))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    idem, err = _require_idempotency(idempotency_key, request_id)
    if err:
        return err
    endpoint = f"POST:/applications/{application_id}/score"
    repo = ApplicationRepository(db)
    cached = repo.get_idempotency_response(endpoint, idem)
    if cached:
        return cached

    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")

    if not app.interview_sessions:
        return error_response(
            request_id=request_id,
            status_code=409,
            code="CONFLICT",
            message="application is not ready for scoring",
            details=[{"field": "status", "issue": "start interview session before scoring"}],
        )

    service = ApplicationService(repo, EventPublisher(db))
    trace_id = service.make_trace_id(request_id)
    result = service.trigger_scoring(
        application_id=application_id,
        mode=req.mode,
        dry_run=req.dry_run,
        trace_id=trace_id,
        idempotency_key=idem,
        transcript_text=req.transcript_text,
        transcript_uri=req.transcript_uri,
    )
    response_payload = envelope(request_id=request_id, data=result)
    repo.save_idempotency_response(endpoint, idem, response_payload)
    db.commit()
    audit_log("scoring_enqueue", request, "allowed", {"application_id": application_id, "job_id": result["job_id"]})
    return response_payload


@router.get("/{application_id}/decision")
def get_decision(
    application_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    founder=Depends(current_founder),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        # defensive fallback; FastAPI should always provide Request
        raise RuntimeError("request_context_missing")
    if founder is None:
        denied = enforce_roles(request, ("viewer", "analyst", "admin"))
        if denied:
            return denied
    request_id = x_request_id or new_request_id()
    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")
    if not _founder_owns_application(founder, app):
        return error_response(request_id, 403, "FORBIDDEN", "application not owned by caller")

    service = ApplicationService(repo, EventPublisher(db))
    result = service.get_decision(application_id)
    return envelope(request_id=request_id, data=result)


@router.get("/{application_id}/decision_artifact")
def get_decision_artifact(
    application_id: str = Path(...),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Return the persisted ``decision_artifact.v1`` bundle (prompt 17).

    Wraps the canonical artifact payload in the standard envelope.
    Enforces ``viewer | analyst | admin`` roles per the OpenAPI
    contract (``x-required-roles``).
    """
    if request is None:
        raise RuntimeError("request_context_missing")
    denied = enforce_roles(request, ("viewer", "analyst", "admin"))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")

    row = (
        db.query(models.ArgumentArtifact)
        .filter(
            models.ArgumentArtifact.application_id == application_id,
            models.ArgumentArtifact.kind == ARTIFACT_KIND,
        )
        .order_by(models.ArgumentArtifact.created_at.desc())
        .first()
    )
    if row is None or not row.payload_json:
        return error_response(
            request_id, 404, "NOT_FOUND", "decision_artifact not available"
        )
    try:
        payload: Dict[str, Any] = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return error_response(
            request_id, 500, "INTERNAL_ERROR", "decision_artifact payload corrupted"
        )
    decision_policy_version = ""
    try:
        decision_policy_version = str(
            payload.get("decision", {}).get("policy_version", "")
        )
    except AttributeError:
        decision_policy_version = ""
    data = {
        "application_id": application_id,
        "artifact_id": row.id,
        "kind": row.kind,
        "decision_policy_version": decision_policy_version,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "payload": payload,
    }
    return envelope(request_id=request_id, data=data)


@router.post("/{application_id}/mode")
def set_scoring_mode(
    request: Request,
    application_id: str = Path(...),
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Toggle scoring mode between ``enforce`` and ``shadow`` (prompt 17).

    Delegates to :meth:`ApplicationService.set_scoring_mode` which
    enforces the prompt 12 guardrail refusing an
    ``enforce -> shadow`` transition once a decision has been
    issued, unless ``force=true`` is passed.
    """
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    new_mode = str(body.get("mode", "")).strip().lower()
    if new_mode not in {"enforce", "shadow"}:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "mode must be 'enforce' or 'shadow'",
            details=[{"field": "mode", "issue": "invalid_value"}],
        )
    force = bool(body.get("force", False))
    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")
    service = ApplicationService(repo, EventPublisher(db))
    try:
        result = service.set_scoring_mode(
            application_id, new_mode=new_mode, force=force
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "application_not_found":
            return error_response(request_id, 404, "NOT_FOUND", "application not found")
        return error_response(request_id, 422, "VALIDATION_ERROR", msg)
    except RuntimeError as exc:
        if str(exc) == "enforce_to_shadow_forbidden_after_decision_issued":
            return error_response(
                request_id,
                422,
                "UNPROCESSABLE_STATE",
                "enforce->shadow forbidden after a decision has been issued; set force=true to override",
            )
        return error_response(request_id, 500, "INTERNAL_ERROR", "mode toggle failed")
    db.commit()
    audit_log(
        "scoring_mode_set",
        request,
        "allowed",
        {"application_id": application_id, "new_mode": new_mode, "force": force},
    )
    return envelope(request_id=request_id, data=result)


@router.post("/{application_id}/escalation-packet", status_code=201)
def create_escalation_packet(
    req: CreateEscalationPacketRequest,
    request: Request,
    application_id: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    idem, err = _require_idempotency(idempotency_key, request_id)
    if err:
        return err
    endpoint = f"POST:/applications/{application_id}/escalation-packet"
    repo = ApplicationRepository(db)
    cached = repo.get_idempotency_response(endpoint, idem)
    if cached:
        return cached

    service = ApplicationService(repo, EventPublisher(db))
    try:
        result = service.create_escalation_packet(
            application_id=application_id,
            partner_email=str(req.partner_email),
            include_calendar_link=req.include_calendar_link,
        )
    except RuntimeError as ex:
        msg = str(ex)
        if msg == "decision_not_available":
            return error_response(request_id, 422, "UNPROCESSABLE_STATE", "decision not available")
        if msg.startswith("decision_not_pass:"):
            decision = msg.split(":", 1)[1]
            return error_response(
                request_id=request_id,
                status_code=422,
                code="UNPROCESSABLE_STATE",
                message="escalation allowed only for pass decisions",
                details=[{"field": "decision", "issue": f"current decision is {decision}"}],
            )
        return error_response(request_id, 500, "INTERNAL_ERROR", "unexpected escalation error")

    response_payload = envelope(
        request_id=request_id,
        data={"packet_id": result["packet_id"], "packet_uri": result["packet_uri"], "status": result["status"]},
    )
    repo.save_idempotency_response(endpoint, idem, response_payload)
    db.commit()
    audit_log("escalation_packet_create", request, "allowed", {"application_id": application_id, "packet_id": result["packet_id"]})
    return response_payload

