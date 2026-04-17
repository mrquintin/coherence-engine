"""Workflow orchestrator + notification log routers (prompt 17).

Thin FastAPI endpoints that delegate to the existing workflow
orchestrator (prompt 15) and the notification log (prompt 14).
No business logic lives here: each handler enforces roles,
delegates to a service, and wraps the result in the standard
envelope.

Exposed endpoints (mounted at ``/api/v1`` by the application):

* ``POST /workflow/{application_id}/run``   (analyst | admin)
* ``POST /workflow/{application_id}/resume`` (analyst | admin)
* ``GET  /notifications?application_id=...`` (viewer | analyst | admin)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import envelope, error_response, new_request_id
from coherence_engine.server.fund.database import SessionLocal, get_db
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository
from coherence_engine.server.fund.security import audit_log, enforce_roles
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.application_service import ApplicationService
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.services.workflow import (
    WorkflowError,
    WorkflowResumeRefused,
)

router = APIRouter(tags=["workflow"])


def _attach_principal(request: Request) -> None:
    """Ensure ``request.state.principal`` is populated.

    The production :class:`FundSecurityMiddleware` restricts principal
    attachment to known fund path prefixes (``/applications``,
    ``/admin/api-keys``). The new ``/workflow`` and ``/notifications``
    surfaces added by prompt 17 therefore must perform the same API-key
    verification themselves so :func:`enforce_roles` can gate on role.

    This helper is intentionally a superset of the middleware:
    identical verification semantics against the API-key repository,
    but scoped to per-request use inside this router.
    """
    if getattr(request.state, "principal", None):
        return
    token: Optional[str] = None
    raw_key = request.headers.get("x-api-key")
    if raw_key:
        token = raw_key.strip()
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        verification = svc.verify_token(repo, token)
        if not verification.get("ok"):
            db.rollback()
            return
        request.state.principal = {
            "auth_type": "api_key_db",
            "token_fingerprint": verification["fingerprint"],
            "fingerprint": verification["fingerprint"],
            "role": verification["role"],
            "key_id": verification["key_id"],
        }
        db.commit()
    finally:
        db.close()


def _serialize_run(session: Session, run: models.WorkflowRun) -> Dict[str, Any]:
    steps = (
        session.query(models.WorkflowStep)
        .filter(models.WorkflowStep.workflow_run_id == run.id)
        .order_by(models.WorkflowStep.created_at.asc())
        .all()
    )
    return {
        "run_id": run.id,
        "application_id": run.application_id,
        "status": run.status,
        "current_step": run.current_step or "",
        "error": run.error or "",
        "steps": [
            {
                "name": s.name,
                "status": s.status,
                "input_digest": s.input_digest or "",
                "output_digest": s.output_digest or "",
                "error": s.error or "",
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            }
            for s in steps
        ],
    }


def _run_or_resume(
    *,
    request: Request,
    application_id: str,
    db: Session,
    x_request_id: Optional[str],
    resume: bool,
    force: bool,
):
    _attach_principal(request)
    denied = enforce_roles(request, ("analyst", "admin"))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")

    service = ApplicationService(repo, EventPublisher(db))
    try:
        run = service.run_application_workflow(
            application_id,
            resume=resume,
            force=force,
            trace_id=f"api-{request_id}",
        )
    except WorkflowResumeRefused as exc:
        return error_response(
            request_id,
            409,
            "CONFLICT",
            "workflow resume refused due to input digest drift; pass force=true to override",
            details=[{"field": "input_digest", "issue": str(exc)}],
        )
    except WorkflowError as exc:
        return error_response(request_id, 422, "UNPROCESSABLE_STATE", str(exc))
    except Exception as exc:  # noqa: BLE001 — surface stage failure to caller.
        db.commit()  # persist the failed WorkflowRun/WorkflowStep rows.
        # Re-fetch the failed run so the client can see which stage broke.
        latest = (
            db.query(models.WorkflowRun)
            .filter(models.WorkflowRun.application_id == application_id)
            .order_by(models.WorkflowRun.started_at.desc())
            .first()
        )
        payload: Dict[str, Any] = {"error_class": type(exc).__name__, "error_message": str(exc)}
        if latest is not None:
            payload["run"] = _serialize_run(db, latest)
        return error_response(
            request_id,
            500,
            "WORKFLOW_STAGE_FAILED",
            f"workflow stage raised: {type(exc).__name__}",
            details=[payload],
        )

    data = _serialize_run(db, run)
    db.commit()
    audit_log(
        "workflow_run" if not resume else "workflow_resume",
        request,
        "allowed",
        {
            "application_id": application_id,
            "run_id": data["run_id"],
            "status": data["status"],
            "force": force,
        },
    )
    return envelope(request_id=request_id, data=data)


@router.post("/workflow/{application_id}/run", status_code=202)
def run_workflow_endpoint(
    request: Request,
    application_id: str = Path(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Start a fresh workflow orchestrator run (prompt 17)."""
    return _run_or_resume(
        request=request,
        application_id=application_id,
        db=db,
        x_request_id=x_request_id,
        resume=False,
        force=False,
    )


@router.post("/workflow/{application_id}/resume", status_code=202)
def resume_workflow_endpoint(
    request: Request,
    application_id: str = Path(...),
    body: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Resume the most recent non-succeeded workflow run (prompt 17)."""
    force = bool((body or {}).get("force", False))
    return _run_or_resume(
        request=request,
        application_id=application_id,
        db=db,
        x_request_id=x_request_id,
        resume=True,
        force=force,
    )


@router.get("/notifications")
def list_notifications_endpoint(
    request: Request,
    application_id: str = Query(..., description="Application identifier to filter by"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Return paginated :class:`NotificationLog` entries (prompt 17)."""
    _attach_principal(request)
    denied = enforce_roles(request, ("viewer", "analyst", "admin"))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()

    base = db.query(models.NotificationLog).filter(
        models.NotificationLog.application_id == application_id
    )
    total = base.count()
    rows = (
        base.order_by(models.NotificationLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    entries = [
        {
            "id": r.id,
            "application_id": r.application_id,
            "template_id": r.template_id,
            "channel": r.channel,
            "recipient": r.recipient,
            "status": r.status,
            "error": r.error or "",
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        }
        for r in rows
    ]
    return envelope(
        request_id=request_id,
        data={
            "application_id": application_id,
            "entries": entries,
            "limit": limit,
            "offset": offset,
            "total": total,
        },
    )
