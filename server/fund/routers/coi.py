"""Conflict-of-interest registry router (prompt 59).

Surface:

* ``POST /coi/declarations`` -- partner declares (or updates) a
  conflict. Partner / admin role required.
* ``GET /coi/declarations`` -- list declarations. Partners see their
  own only; admins see all (optionally filtered by ``partner_id``).
* ``POST /coi/check`` -- run :func:`check_coi` for a given
  ``(application_id, partner_id)`` pair on demand. Useful for the
  partner dashboard preview before a meeting is auto-booked.
* ``POST /coi/override`` -- admin records an override with a
  ≥ 50-character justification. The endpoint deliberately never
  auto-clears anything (prompt 59 prohibition); a row in
  ``fund_coi_overrides`` is the disclosure trail.

Every write path emits an :func:`audit_log` event so the disclosure
trail is auditable end-to-end.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import (
    envelope,
    error_response,
    new_request_id,
)
from coherence_engine.server.fund.database import SessionLocal, get_db
from coherence_engine.server.fund.repositories.api_key_repository import (
    ApiKeyRepository,
)
from coherence_engine.server.fund.security import audit_log, enforce_roles
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.conflict_of_interest import (
    COIError,
    MIN_OVERRIDE_JUSTIFICATION_LENGTH,
    VALID_PARTY_KINDS,
    VALID_RELATIONSHIPS,
    check_coi,
    record_override,
)


router = APIRouter(prefix="/coi", tags=["coi"])
LOGGER = logging.getLogger("coherence_engine.fund.coi")


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _attach_principal(request: Request) -> None:
    """Mirror the partner_api glue: COI paths are not in
    ``_is_fund_path`` so the security middleware does not stamp a
    principal for us. Pull the token from the request, verify via
    :class:`ApiKeyService`, and stamp ``request.state.principal``
    so :func:`enforce_roles` sees the role.
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


def _principal(request: Request) -> Dict[str, Any]:
    _attach_principal(request)
    return getattr(request.state, "principal", None) or {}


def _principal_id(request: Request) -> str:
    principal = _principal(request)
    return str(
        principal.get("id")
        or principal.get("subject")
        or principal.get("api_key_id")
        or principal.get("partner_id")
        or principal.get("email")
        or "unknown"
    )


def _principal_role(request: Request) -> str:
    return str(_principal(request).get("role", "")).lower()


# ---------------------------------------------------------------------------
# POST /coi/declarations -- declare or update a conflict
# ---------------------------------------------------------------------------


@router.post("/declarations", status_code=201)
def declare_coi_route(
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    _attach_principal(request)
    denied = enforce_roles(request, ("partner", "admin"))
    if denied:
        return denied

    role = _principal_role(request)
    actor_id = _principal_id(request)

    # Partners may only declare on themselves; admins may target any.
    body_partner = str(body.get("partner_id") or "").strip()
    if role == "partner":
        partner_id = actor_id
    else:
        partner_id = body_partner or actor_id
    if not partner_id:
        return error_response(
            request_id, 422, "MISSING_PARTNER", "partner_id is required"
        )

    party_kind = str(body.get("party_kind") or "company").strip().lower()
    if party_kind not in VALID_PARTY_KINDS:
        return error_response(
            request_id,
            422,
            "INVALID_PARTY_KIND",
            f"party_kind must be one of {sorted(VALID_PARTY_KINDS)}",
        )
    party_id_ref = str(body.get("party_id_ref") or "").strip()
    if not party_id_ref:
        return error_response(
            request_id,
            422,
            "MISSING_PARTY_REF",
            "party_id_ref is required",
        )

    relationship = str(body.get("relationship") or "").strip().lower()
    if relationship not in VALID_RELATIONSHIPS:
        return error_response(
            request_id,
            422,
            "INVALID_RELATIONSHIP",
            f"relationship must be one of {sorted(VALID_RELATIONSHIPS)}",
        )

    period_start = _parse_dt(body.get("period_start")) or _utc_now()
    period_end = _parse_dt(body.get("period_end"))
    if period_end is not None and period_end <= period_start:
        return error_response(
            request_id,
            422,
            "INVALID_PERIOD",
            "period_end must be after period_start",
        )

    decl = models.COIDeclaration(
        id=f"coid_{uuid.uuid4().hex[:24]}",
        partner_id=partner_id,
        party_kind=party_kind,
        party_id_ref=party_id_ref,
        relationship=relationship,
        period_start=period_start,
        period_end=period_end,
        evidence_uri=str(body.get("evidence_uri") or "").strip(),
        note=str(body.get("note") or "").strip(),
        status="active",
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(decl)
    db.commit()

    audit_log(
        event="coi_declaration_created",
        request=request,
        outcome="allowed",
        details={
            "declaration_id": decl.id,
            "partner_id": partner_id,
            "relationship": relationship,
            "party_id_ref": party_id_ref,
        },
    )

    return envelope(
        request_id=request_id,
        data={
            "declaration_id": decl.id,
            "partner_id": partner_id,
            "party_kind": party_kind,
            "party_id_ref": party_id_ref,
            "relationship": relationship,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat() if period_end else None,
            "status": decl.status,
        },
    )


# ---------------------------------------------------------------------------
# GET /coi/declarations -- list
# ---------------------------------------------------------------------------


@router.get("/declarations")
def list_coi_declarations_route(
    partner_id: Optional[str] = Query(default=None),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    _attach_principal(request)
    denied = enforce_roles(request, ("partner", "admin", "viewer"))
    if denied:
        return denied

    role = _principal_role(request)
    actor_id = _principal_id(request)

    q = db.query(models.COIDeclaration)
    if role == "partner":
        # Partners are scoped to their own rows.
        q = q.filter(models.COIDeclaration.partner_id == actor_id)
    elif partner_id:
        q = q.filter(models.COIDeclaration.partner_id == str(partner_id).strip())

    rows = q.order_by(models.COIDeclaration.created_at.desc()).all()
    return envelope(
        request_id=request_id,
        data={
            "declarations": [
                {
                    "id": r.id,
                    "partner_id": r.partner_id,
                    "party_kind": r.party_kind,
                    "party_id_ref": r.party_id_ref,
                    "relationship": r.relationship,
                    "period_start": r.period_start.isoformat() if r.period_start else None,
                    "period_end": r.period_end.isoformat() if r.period_end else None,
                    "evidence_uri": r.evidence_uri or "",
                    "note": r.note or "",
                    "status": r.status,
                }
                for r in rows
            ],
        },
    )


# ---------------------------------------------------------------------------
# POST /coi/check -- run a check on demand
# ---------------------------------------------------------------------------


@router.post("/check")
def check_coi_route(
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    _attach_principal(request)
    denied = enforce_roles(request, ("partner", "admin", "analyst"))
    if denied:
        return denied

    application_id = str(body.get("application_id") or "").strip()
    partner_id = str(body.get("partner_id") or "").strip()
    if not application_id or not partner_id:
        return error_response(
            request_id,
            422,
            "VALIDATION_ERROR",
            "application_id and partner_id are required",
        )

    application_row = (
        db.query(models.Application)
        .filter(models.Application.id == application_id)
        .one_or_none()
    )
    if application_row is None:
        return error_response(
            request_id, 404, "NOT_FOUND", "application not found"
        )
    founder_row = (
        db.query(models.Founder)
        .filter(models.Founder.id == application_row.founder_id)
        .one_or_none()
    )
    application_view: Dict[str, Any] = {
        "id": application_row.id,
        "founder_id": application_row.founder_id,
    }
    if founder_row is not None:
        application_view.update(
            {
                "founder_user_id": founder_row.founder_user_id or "",
                "founder_email_token": founder_row.email_token or "",
                "company_name": founder_row.company_name or "",
            }
        )

    try:
        result = check_coi(db, application_view, partner_id)
    except COIError as exc:
        return error_response(
            request_id, exc.http_status, exc.code, exc.message
        )
    db.commit()

    audit_log(
        event="coi_check",
        request=request,
        outcome="allowed",
        details={
            "application_id": application_id,
            "partner_id": partner_id,
            "status": result.status,
        },
    )

    return envelope(request_id=request_id, data=result.to_dict())


# ---------------------------------------------------------------------------
# POST /coi/override -- admin override with justification
# ---------------------------------------------------------------------------


@router.post("/override", status_code=201)
def override_coi_route(
    body: Dict[str, Any] = Body(default_factory=dict),
    request: Request = None,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    if request is None:
        raise RuntimeError("request_context_missing")
    request_id = x_request_id or new_request_id()
    _attach_principal(request)
    denied = enforce_roles(request, ("admin",))
    if denied:
        return denied

    application_id = str(body.get("application_id") or "").strip()
    partner_id = str(body.get("partner_id") or "").strip()
    justification = body.get("justification") or ""
    overridden_by = _principal_id(request)

    try:
        row = record_override(
            db,
            application_id=application_id,
            partner_id=partner_id,
            justification=justification,
            overridden_by=overridden_by,
        )
    except COIError as exc:
        return error_response(
            request_id, exc.http_status, exc.code, exc.message
        )

    db.commit()
    audit_log(
        event="coi_override",
        request=request,
        outcome="allowed",
        details={
            "override_id": row.id,
            "application_id": application_id,
            "partner_id": partner_id,
            "justification_len": len(row.justification or ""),
        },
    )

    return envelope(
        request_id=request_id,
        data={
            "override_id": row.id,
            "application_id": row.application_id,
            "partner_id": row.partner_id,
            "overridden_by": row.overridden_by,
            "justification_chars": len(row.justification or ""),
            "min_required_chars": MIN_OVERRIDE_JUSTIFICATION_LENGTH,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
    )
