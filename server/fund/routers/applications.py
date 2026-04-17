"""Application routers for fund workflow."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

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
from coherence_engine.server.fund.services.application_service import ApplicationService
from coherence_engine.server.fund.services.decision_artifact import ARTIFACT_KIND
from coherence_engine.server.fund.services.event_publisher import EventPublisher
from coherence_engine.server.fund.security import audit_log, enforce_roles

router = APIRouter(prefix="/applications", tags=["applications"])


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
    response_payload = envelope(
        request_id=request_id,
        data={"application_id": ids["application_id"], "founder_id": ids["founder_id"], "status": "intake_created"},
    )
    repo.save_idempotency_response(endpoint, idem, response_payload)
    db.commit()
    audit_log("application_create", request, "allowed", {"application_id": ids["application_id"]})
    return response_payload


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
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        # defensive fallback; FastAPI should always provide Request
        raise RuntimeError("request_context_missing")
    denied = enforce_roles(request, ("viewer", "analyst", "admin"))
    if denied:
        return denied
    request_id = x_request_id or new_request_id()
    repo = ApplicationRepository(db)
    app = repo.get_application(application_id)
    if not app:
        return error_response(request_id, 404, "NOT_FOUND", "application not found")

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

