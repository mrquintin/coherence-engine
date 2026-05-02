"""Partner-dashboard API surface (prompt 35).

Mounted at ``/partner`` (and ``/api/v1/partner``) by
:mod:`server.fund.app`. The router consumes the same token-based
authentication as the rest of the fund API but additionally requires
the principal carry role ``partner`` or ``admin``. The partner
dashboard at ``apps/partner_dashboard/`` is the primary client; the
endpoints are JSON-only and follow the standard envelope contract
(``data`` / ``error`` / ``meta``).

Routes:

* ``GET  /partner/pipeline`` — pivot table of in-flight applications.
  Filters: ``domain``, ``verdict``, ``mode``. Pagination is
  cursor-based via ``cursor`` + ``limit`` query parameters; the
  cursor is the opaque ``application_id`` of the last row of the
  prior page.
* ``GET  /partner/applications/{application_id}`` — full application
  detail with the most recent decision artifact and the active
  override (if any).
* ``POST /partner/applications/{application_id}/override`` — write a
  manual override. Body is JSON ``{override_verdict, reason_code,
  reason_text, justification_uri?, unrevise?}``. Idempotent on the
  ``(application_id, status="active")`` pair (see
  :class:`DecisionOverrideService`).
* ``GET  /partner/audit`` — audit-log view (re-uses
  :class:`ApiKeyAuditEvent` rows).

The legacy HTMX admin (prompt 19) at ``/admin`` is preserved as a
fallback per prompt-35 prohibition; nothing here mutates that surface.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
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
from coherence_engine.server.fund.security import audit_log
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.decision_overrides import (
    DecisionOverrideService,
    OverrideError,
    require_role,
)


router = APIRouter(prefix="/partner", tags=["partner"])


PARTNER_ROLES = ("partner", "admin")
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# Auth glue — partner paths are not in :func:`_is_fund_path`, so the
# :class:`FundSecurityMiddleware` does not attach a principal for us.
# We mirror the admin_ui.py pattern: pull the token from the request,
# verify it via the v2 :class:`ApiKeyService`, and stamp
# ``request.state.principal`` so :func:`require_role` works.
# ---------------------------------------------------------------------------


def _attach_principal(request: Request) -> None:
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


def _gate(request: Request) -> Optional[JSONResponse]:
    """Authenticate + authorize. Returns the error response on failure."""

    _attach_principal(request)
    principal = getattr(request.state, "principal", None)
    if not principal:
        request_id = request.headers.get("x-request-id") or new_request_id()
        audit_log(
            event="auth_failure",
            request=request,
            outcome="denied",
            details={"reason": "missing_token", "router": "partner_api"},
        )
        return error_response(
            request_id, 401, "UNAUTHORIZED", "missing or invalid API token"
        )
    return require_role(request, PARTNER_ROLES)


def _request_id(request: Request, header_request_id: Optional[str]) -> str:
    rid = header_request_id or request.headers.get("x-request-id") or new_request_id()
    request.state.request_id = rid
    return rid


# ---------------------------------------------------------------------------
# Pipeline pivot
# ---------------------------------------------------------------------------


def _serialize_application_row(
    app: models.Application,
    decision: Optional[models.Decision],
    override: Optional[models.DecisionOverride],
) -> Dict[str, Any]:
    automated_verdict = decision.decision if decision else ""
    effective_verdict = (
        override.override_verdict if override is not None else automated_verdict
    )
    return {
        "application_id": app.id,
        "founder_id": app.founder_id,
        "domain_primary": app.domain_primary or "",
        "status": app.status or "",
        "scoring_mode": app.scoring_mode or "enforce",
        "automated_verdict": automated_verdict,
        "effective_verdict": effective_verdict,
        "override_active": override is not None,
        "override_reason_code": override.reason_code if override else "",
        "coherence_observed": decision.coherence_observed if decision else None,
        "threshold_required": decision.threshold_required if decision else None,
        "margin": decision.margin if decision else None,
        "created_at": app.created_at.isoformat() if app.created_at else "",
        "updated_at": app.updated_at.isoformat() if app.updated_at else "",
    }


@router.get("/pipeline")
def get_pipeline(
    request: Request,
    db: Session = Depends(get_db),
    domain: Optional[str] = Query(default=None, description="Filter by domain_primary"),
    verdict: Optional[str] = Query(
        default=None,
        description="Filter by effective verdict (pass|reject|manual_review)",
    ),
    mode: Optional[str] = Query(
        default=None, description="Filter by scoring_mode (enforce|shadow)"
    ),
    cursor: Optional[str] = Query(
        default=None, description="Opaque cursor — application_id of last seen row"
    ),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Return a cursor-paginated view of the application pipeline filter."""

    denied = _gate(request)
    if denied is not None:
        return denied
    rid = _request_id(request, x_request_id)

    q = db.query(models.Application)
    if domain:
        q = q.filter(models.Application.domain_primary == domain)
    if mode:
        q = q.filter(models.Application.scoring_mode == mode)
    q = q.order_by(
        models.Application.created_at.desc(), models.Application.id.desc()
    )
    if cursor:
        cur_app = (
            db.query(models.Application)
            .filter(models.Application.id == cursor)
            .one_or_none()
        )
        if cur_app is not None and cur_app.created_at is not None:
            q = q.filter(
                (models.Application.created_at < cur_app.created_at)
                | (
                    (models.Application.created_at == cur_app.created_at)
                    & (models.Application.id < cur_app.id)
                )
            )

    # Over-fetch by 1 to detect ``has_more``.
    rows = q.limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    app_ids = tuple(a.id for a in rows)
    decisions: Dict[str, models.Decision] = {}
    if app_ids:
        for d in (
            db.query(models.Decision)
            .filter(models.Decision.application_id.in_(list(app_ids)))
            .all()
        ):
            decisions[d.application_id] = d

    override_svc = DecisionOverrideService(db)
    overrides = override_svc.list_active_overrides_for(app_ids)

    serialized: List[Dict[str, Any]] = []
    for app in rows:
        item = _serialize_application_row(
            app, decisions.get(app.id), overrides.get(app.id)
        )
        if verdict and item["effective_verdict"] != verdict:
            continue
        serialized.append(item)

    next_cursor = serialized[-1]["application_id"] if (has_more and serialized) else None

    audit_log(
        event="partner_pipeline_view",
        request=request,
        outcome="allowed",
        details={
            "filter_domain": domain or "",
            "filter_verdict": verdict or "",
            "filter_mode": mode or "",
            "result_count": len(serialized),
        },
    )
    return JSONResponse(
        content=envelope(
            request_id=rid,
            data={
                "items": serialized,
                "next_cursor": next_cursor,
                "has_more": bool(has_more),
                "filter": {
                    "domain": domain or "",
                    "verdict": verdict or "",
                    "mode": mode or "",
                },
            },
        )
    )


# ---------------------------------------------------------------------------
# Application detail
# ---------------------------------------------------------------------------


@router.get("/applications/{application_id}")
def get_application_detail(
    request: Request,
    application_id: str = Path(..., min_length=1, max_length=40),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = _gate(request)
    if denied is not None:
        return denied
    rid = _request_id(request, x_request_id)

    app = (
        db.query(models.Application)
        .filter(models.Application.id == application_id)
        .one_or_none()
    )
    if app is None:
        return error_response(rid, 404, "NOT_FOUND", "application not found")

    decision = (
        db.query(models.Decision)
        .filter(models.Decision.application_id == application_id)
        .one_or_none()
    )
    override_svc = DecisionOverrideService(db)
    override = override_svc.list_active_overrides_for((application_id,)).get(
        application_id
    )

    artifact: Optional[Dict[str, Any]] = None
    if decision is not None:
        try:
            failed_gates = json.loads(decision.failed_gates_json or "[]")
        except (TypeError, ValueError):
            failed_gates = []
        artifact = {
            "decision": decision.decision,
            "policy_version": decision.policy_version,
            "decision_policy_version": decision.decision_policy_version,
            "parameter_set_id": decision.parameter_set_id,
            "threshold_required": decision.threshold_required or 0.0,
            "coherence_observed": decision.coherence_observed or 0.0,
            "margin": decision.margin or 0.0,
            "failed_gates": failed_gates,
        }

    payload = _serialize_application_row(app, decision, override)
    payload["decision_artifact"] = artifact
    payload["override"] = (
        {
            "id": override.id,
            "override_verdict": override.override_verdict,
            "reason_code": override.reason_code,
            "reason_text": override.reason_text,
            "justification_uri": override.justification_uri or "",
            "overridden_by": override.overridden_by,
            "overridden_at": (
                override.overridden_at.isoformat()
                if override.overridden_at
                else ""
            ),
        }
        if override is not None
        else None
    )

    audit_log(
        event="partner_application_view",
        request=request,
        outcome="allowed",
        details={"application_id": application_id},
    )
    return JSONResponse(content=envelope(request_id=rid, data=payload))


# ---------------------------------------------------------------------------
# Override write
# ---------------------------------------------------------------------------


@router.post("/applications/{application_id}/override")
def post_override(
    request: Request,
    application_id: str = Path(..., min_length=1, max_length=40),
    body: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = _gate(request)
    if denied is not None:
        return denied
    rid = _request_id(request, x_request_id)

    principal = getattr(request.state, "principal", None) or {}
    actor = (
        principal.get("fingerprint")
        or principal.get("token_fingerprint")
        or principal.get("role")
        or "unknown"
    )

    override_verdict = str(body.get("override_verdict", "")).strip()
    reason_code = str(body.get("reason_code", "")).strip()
    reason_text = body.get("reason_text", "")
    justification_uri = body.get("justification_uri")
    unrevise = bool(body.get("unrevise", False))

    svc = DecisionOverrideService(db)
    try:
        result = svc.create_override(
            application_id=application_id,
            override_verdict=override_verdict,
            reason_code=reason_code,
            reason_text=reason_text if isinstance(reason_text, str) else "",
            overridden_by=str(actor),
            justification_uri=(
                str(justification_uri).strip()
                if isinstance(justification_uri, str)
                else None
            ),
            unrevise=unrevise,
            trace_id=rid,
        )
    except OverrideError as exc:
        audit_log(
            event="decision_override_rejected",
            request=request,
            outcome="denied",
            details={
                "application_id": application_id,
                "code": exc.code,
                "reason_code": reason_code,
            },
        )
        status_code = 404 if exc.code == "DECISION_NOT_FOUND" else 400
        return error_response(rid, status_code, exc.code, exc.message)

    db.commit()

    override = result.override
    audit_log(
        event="decision_override_applied",
        request=request,
        outcome="allowed",
        details={
            "application_id": application_id,
            "override_id": override.id,
            "created": result.created,
            "superseded_id": result.superseded_id or "",
            "override_verdict": override.override_verdict,
            "original_verdict": override.original_verdict,
            "reason_code": override.reason_code,
        },
    )
    return JSONResponse(
        status_code=201 if result.created else 200,
        content=envelope(
            request_id=rid,
            data={
                "override_id": override.id,
                "application_id": override.application_id,
                "original_verdict": override.original_verdict,
                "override_verdict": override.override_verdict,
                "reason_code": override.reason_code,
                "reason_text": override.reason_text,
                "justification_uri": override.justification_uri or "",
                "overridden_by": override.overridden_by,
                "overridden_at": (
                    override.overridden_at.isoformat()
                    if override.overridden_at
                    else ""
                ),
                "status": override.status,
                "created": result.created,
                "superseded_override_id": result.superseded_id or "",
            },
        ),
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit")
def get_audit_log(
    request: Request,
    db: Session = Depends(get_db),
    application_id: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    denied = _gate(request)
    if denied is not None:
        return denied
    rid = _request_id(request, x_request_id)

    q = db.query(models.ApiKeyAuditEvent).order_by(
        models.ApiKeyAuditEvent.created_at.desc()
    )
    if action:
        q = q.filter(models.ApiKeyAuditEvent.action == action)
    rows = q.limit(limit).all()

    items: List[Dict[str, Any]] = []
    for r in rows:
        try:
            details = json.loads(r.details_json or "{}")
        except (TypeError, ValueError):
            details = {}
        if application_id:
            if str(details.get("application_id", "")) != application_id:
                continue
        items.append(
            {
                "id": r.id,
                "action": r.action,
                "success": bool(r.success),
                "actor": r.actor or "",
                "request_id": r.request_id or "",
                "ip": r.ip or "",
                "path": r.path or "",
                "details": details,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
        )

    return JSONResponse(
        content=envelope(
            request_id=rid,
            data={
                "items": items,
                "filter": {
                    "application_id": application_id or "",
                    "action": action or "",
                },
            },
        )
    )
