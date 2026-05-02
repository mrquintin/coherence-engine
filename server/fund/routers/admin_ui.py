"""Read-only admin dashboard (prompt 19).

Minimal HTMX-driven operator UI served from FastAPI. All routes are
role-gated to ``admin`` via :func:`enforce_roles`; no endpoint here
mutates server state (mutations remain on the CLI / API surface per
the prompt-19 prohibition).

Endpoints (mounted at ``/admin`` by :mod:`server.fund.app`):

* ``GET /admin/applications``
    Paginated list of applications with per-row links to the detail
    view. Full page render; supports ``?page=`` and ``?page_size=``.
* ``GET /admin/applications/{application_id}``
    Per-application summary page. The scoring, workflow and
    notification sections are placeholders that the browser fills in
    via three HTMX ``hx-get`` calls to the fragment endpoints below.
* ``GET /admin/applications/{application_id}/fragment/scores``
    HTML fragment containing the latest decision artifact (if any)
    plus a table of scoring jobs for the application.
* ``GET /admin/applications/{application_id}/fragment/workflow``
    HTML fragment listing every :class:`WorkflowRun` for the
    application and the per-step checkpoint rows.
* ``GET /admin/applications/{application_id}/fragment/notifications``
    HTML fragment listing :class:`NotificationLog` entries.

All fragment endpoints return ``Content-Type: text/html`` and
preserve a stable ``id=\"*-fragment\"`` wrapper element so tests
(and HTMX ``hx-target=\"...\"`` selectors) can assert on the
fragment contract.

Static assets (vendored HTMX + admin.css) are served by FastAPI
:class:`StaticFiles` mounted at ``/admin/static`` from
``server/fund/static/admin/`` (see :mod:`server.fund.app`). There is
no CDN reference anywhere in the templates (prompt 19 prohibition).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, Path as FPath, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.api_utils import new_request_id
from coherence_engine.server.fund.database import SessionLocal, get_db
from coherence_engine.server.fund.repositories.api_key_repository import (
    ApiKeyRepository,
)
from coherence_engine.server.fund.services.api_key_service import ApiKeyService


# ---------------------------------------------------------------------------
# Router + template configuration
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/admin", tags=["admin-ui"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

ADMIN_BASE_PATH = "/admin"
ADMIN_STATIC_PATH = "/admin/static"

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Auth glue (same pattern as routers/workflow.py from prompt 17)
# ---------------------------------------------------------------------------


def _attach_principal(request: Request) -> None:
    """Populate ``request.state.principal`` for admin UI routes.

    :class:`FundSecurityMiddleware` only attaches a principal to known
    fund path prefixes (``/applications`` and ``/admin/api-keys``).
    The admin-UI surface at ``/admin/applications`` therefore must
    perform the same token verification so :func:`enforce_roles` can
    gate on role. The helper is intentionally a superset of the
    middleware — identical semantics, scoped to per-request use
    inside this router — and is safe to call on every request.
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


def _unauthorized_html(request: Request, *, missing_token: bool) -> HTMLResponse:
    """Return a small ``text/html`` error body for unauth / forbid.

    The ``FundSecurityMiddleware`` emits JSON for fund API paths; the
    admin dashboard responds in HTML so browsers don't see a raw
    JSON envelope. The status codes match the middleware contract
    (``401`` for missing / invalid token; ``403`` for insufficient
    role).
    """

    if missing_token:
        status = 401
        title = "401 Unauthorized"
        message = "Missing or invalid admin API token."
    else:
        status = 403
        title = "403 Forbidden"
        message = "Admin role required to view this page."
    body = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
        f"<title>{title}</title></head><body>"
        f"<h1>{title}</h1><p>{message}</p>"
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=status)


def _require_admin(request: Request) -> Optional[HTMLResponse]:
    """Verify the current request is an authenticated admin.

    Returns ``None`` on success, or an :class:`HTMLResponse` carrying
    the appropriate ``401``/``403`` status code otherwise. The caller
    should short-circuit and return the response as-is.
    """

    _attach_principal(request)
    principal = getattr(request.state, "principal", None)
    if not principal:
        return _unauthorized_html(request, missing_token=True)
    role = str(principal.get("role", "")).lower()
    if role != "admin":
        return _unauthorized_html(request, missing_token=False)
    return None


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


def _iso(dt: Any) -> str:
    if dt is None:
        return ""
    try:
        return dt.isoformat()
    except AttributeError:
        return str(dt)


def _base_context(request: Request) -> Dict[str, Any]:
    """Shared Jinja context for every admin-UI template render."""

    principal = getattr(request.state, "principal", None) or {}
    return {
        "request": request,
        "admin_base": ADMIN_BASE_PATH,
        "static_url_prefix": ADMIN_STATIC_PATH,
        "principal_role": principal.get("role", ""),
        "request_id": request.headers.get("x-request-id", ""),
    }


def _application_row(
    app: models.Application,
    founder: Optional[models.Founder],
    decision: Optional[models.Decision],
) -> Dict[str, Any]:
    return {
        "id": app.id,
        "founder_id": app.founder_id,
        "founder_name": getattr(founder, "full_name", "") if founder else "",
        "company_name": getattr(founder, "company_name", "") if founder else "",
        "one_liner": app.one_liner or "",
        "domain_primary": app.domain_primary or "",
        "compliance_status": app.compliance_status or "",
        "status": app.status or "",
        "scoring_mode": app.scoring_mode or "enforce",
        "requested_check_usd": app.requested_check_usd or 0,
        "decision": decision.decision if decision else "",
        "created_at": _iso(app.created_at),
        "updated_at": _iso(app.updated_at),
    }


def _render_block(
    template_name: str,
    block_name: str,
    context: Dict[str, Any],
) -> str:
    """Render a single named block from a Jinja2 template.

    Used by the HTMX fragment endpoints: the fragment markup is
    defined as a ``{% block fragment_* %}`` inside
    ``templates/admin/application_detail.html`` and rendered on
    demand here, avoiding a proliferation of small partial templates
    (SCOPE is three templates total).
    """

    template = templates.get_template(template_name)
    ctx = template.new_context(context)
    parts = []
    for chunk in template.blocks[block_name](ctx):
        parts.append(chunk)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/applications", response_class=HTMLResponse)
def list_applications(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1, description="1-indexed page number."),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description="Rows per page (1..100).",
    ),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Paginated read-only list of applications.

    Returns a full HTML page. Pagination links use HTMX ``hx-get``
    with ``hx-target`` on the applications panel so the operator
    navigates pages without a full page reload; non-JS clients fall
    through to the standard ``href``.
    """

    denied = _require_admin(request)
    if denied is not None:
        return denied
    # Keep a request id in state for audit continuity (even though the
    # admin UI does not mutate, we still stamp a request id for log
    # correlation with the API envelope convention).
    request.state.request_id = x_request_id or new_request_id()

    total = (
        db.query(models.Application)
        .count()
    )
    rows = (
        db.query(models.Application)
        .order_by(models.Application.created_at.desc(), models.Application.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    founder_ids = {a.founder_id for a in rows if a.founder_id}
    founders: Dict[str, models.Founder] = {}
    if founder_ids:
        for f in (
            db.query(models.Founder)
            .filter(models.Founder.id.in_(list(founder_ids)))
            .all()
        ):
            founders[f.id] = f
    app_ids = [a.id for a in rows]
    decisions: Dict[str, models.Decision] = {}
    if app_ids:
        for d in (
            db.query(models.Decision)
            .filter(models.Decision.application_id.in_(app_ids))
            .all()
        ):
            decisions[d.application_id] = d

    applications = [
        _application_row(a, founders.get(a.founder_id), decisions.get(a.id))
        for a in rows
    ]

    context = _base_context(request)
    context.update(
        {
            "applications": applications,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": (page * page_size) < total,
        }
    )
    return templates.TemplateResponse(
        request,
        "admin/applications.html",
        context,
    )


def _load_application_or_404(
    db: Session,
    application_id: str,
) -> Optional[models.Application]:
    return (
        db.query(models.Application)
        .filter(models.Application.id == application_id)
        .one_or_none()
    )


def _detail_application_ctx(
    db: Session,
    app: models.Application,
) -> Dict[str, Any]:
    founder = (
        db.query(models.Founder)
        .filter(models.Founder.id == app.founder_id)
        .one_or_none()
    )
    decision = (
        db.query(models.Decision)
        .filter(models.Decision.application_id == app.id)
        .one_or_none()
    )
    row = _application_row(app, founder, decision)
    return row


@router.get("/applications/{application_id}", response_class=HTMLResponse)
def application_detail(
    request: Request,
    application_id: str = FPath(..., min_length=1, max_length=40),
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    """Per-application detail page.

    The scoring / workflow / notification sections are empty
    placeholders that the HTMX client fills in by calling the three
    fragment endpoints below.
    """

    denied = _require_admin(request)
    if denied is not None:
        return denied
    request.state.request_id = x_request_id or new_request_id()

    app = _load_application_or_404(db, application_id)
    if app is None:
        return HTMLResponse(
            content=(
                "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
                "<title>404 Not Found</title></head><body>"
                "<h1>404 Not Found</h1>"
                f"<p>Application <code>{application_id}</code> does not exist.</p>"
                "<p><a href=\"/admin/applications\">&larr; back to applications</a></p>"
                "</body></html>"
            ),
            status_code=404,
        )

    context = _base_context(request)
    context["application"] = _detail_application_ctx(db, app)
    return templates.TemplateResponse(
        request,
        "admin/application_detail.html",
        context,
    )


# ---------------------------------------------------------------------------
# HTMX fragments
# ---------------------------------------------------------------------------


def _fragment_response(
    request: Request,
    block_name: str,
    extra_context: Dict[str, Any],
) -> HTMLResponse:
    ctx = _base_context(request)
    ctx.update(extra_context)
    html = _render_block("admin/application_detail.html", block_name, ctx)
    return HTMLResponse(content=html, status_code=200, media_type="text/html")


@router.get(
    "/applications/{application_id}/fragment/scores",
    response_class=HTMLResponse,
)
def fragment_scores(
    request: Request,
    application_id: str = FPath(..., min_length=1, max_length=40),
    db: Session = Depends(get_db),
):
    """Scoring-jobs + decision-artifact HTMX fragment."""

    denied = _require_admin(request)
    if denied is not None:
        return denied
    app = _load_application_or_404(db, application_id)
    if app is None:
        return HTMLResponse(
            content=(
                "<div id=\"scores-fragment\" class=\"fragment\">"
                "<p class=\"fragment-empty\">Application not found.</p>"
                "</div>"
            ),
            status_code=404,
            media_type="text/html",
        )

    jobs = (
        db.query(models.ScoringJob)
        .filter(models.ScoringJob.application_id == application_id)
        .order_by(models.ScoringJob.created_at.desc())
        .all()
    )
    decision = (
        db.query(models.Decision)
        .filter(models.Decision.application_id == application_id)
        .one_or_none()
    )

    scoring_jobs = [
        {
            "id": j.id,
            "mode": j.mode or "enforce",
            "status": j.status or "",
            "attempts": j.attempts or 0,
            "max_attempts": j.max_attempts or 0,
            "started_at": _iso(j.started_at),
            "completed_at": _iso(j.completed_at),
            "error_message": (j.error_message or "").strip(),
        }
        for j in jobs
    ]

    artifact: Optional[Dict[str, Any]] = None
    if decision is not None:
        # failed_gates_json is stored as a JSON-encoded string; we
        # surface it verbatim so the operator can read the raw array
        # without round-tripping through a schema. If parsing fails we
        # leave the raw value for human inspection.
        raw_gates = decision.failed_gates_json or "[]"
        try:
            json.loads(raw_gates)
            gates_display = raw_gates
        except (TypeError, ValueError):
            gates_display = raw_gates
        artifact = {
            "decision": decision.decision,
            "policy_version": decision.policy_version,
            "parameter_set_id": decision.parameter_set_id,
            "threshold_required": decision.threshold_required or 0.0,
            "coherence_observed": decision.coherence_observed or 0.0,
            "margin": decision.margin or 0.0,
            "failed_gates_json": gates_display,
        }

    return _fragment_response(
        request,
        "fragment_scores",
        {"scoring_jobs": scoring_jobs, "artifact": artifact, "application_id": application_id},
    )


@router.get(
    "/applications/{application_id}/fragment/workflow",
    response_class=HTMLResponse,
)
def fragment_workflow(
    request: Request,
    application_id: str = FPath(..., min_length=1, max_length=40),
    db: Session = Depends(get_db),
):
    """Workflow-run + per-step HTMX fragment."""

    denied = _require_admin(request)
    if denied is not None:
        return denied
    app = _load_application_or_404(db, application_id)
    if app is None:
        return HTMLResponse(
            content=(
                "<div id=\"workflow-fragment\" class=\"fragment\">"
                "<p class=\"fragment-empty\">Application not found.</p>"
                "</div>"
            ),
            status_code=404,
            media_type="text/html",
        )

    runs = (
        db.query(models.WorkflowRun)
        .filter(models.WorkflowRun.application_id == application_id)
        .order_by(models.WorkflowRun.started_at.desc())
        .all()
    )
    run_ids = [r.id for r in runs]
    steps_by_run: Dict[str, List[models.WorkflowStep]] = {}
    if run_ids:
        step_rows = (
            db.query(models.WorkflowStep)
            .filter(models.WorkflowStep.workflow_run_id.in_(run_ids))
            .order_by(models.WorkflowStep.created_at.asc())
            .all()
        )
        for s in step_rows:
            steps_by_run.setdefault(s.workflow_run_id, []).append(s)

    workflow_runs: List[Dict[str, Any]] = []
    for r in runs:
        workflow_runs.append(
            {
                "id": r.id,
                "status": r.status or "",
                "current_step": r.current_step or "",
                "started_at": _iso(r.started_at),
                "finished_at": _iso(r.finished_at),
                "error": (r.error or "").strip(),
                "steps": [
                    {
                        "name": s.name,
                        "status": s.status or "",
                        "started_at": _iso(s.started_at),
                        "finished_at": _iso(s.finished_at),
                        "input_digest": s.input_digest or "",
                        "output_digest": s.output_digest or "",
                        "error": (s.error or "").strip(),
                    }
                    for s in steps_by_run.get(r.id, [])
                ],
            }
        )

    return _fragment_response(
        request,
        "fragment_workflow",
        {"workflow_runs": workflow_runs, "application_id": application_id},
    )


@router.get(
    "/applications/{application_id}/fragment/notifications",
    response_class=HTMLResponse,
)
def fragment_notifications(
    request: Request,
    application_id: str = FPath(..., min_length=1, max_length=40),
    db: Session = Depends(get_db),
):
    """Notification-log HTMX fragment."""

    denied = _require_admin(request)
    if denied is not None:
        return denied
    app = _load_application_or_404(db, application_id)
    if app is None:
        return HTMLResponse(
            content=(
                "<div id=\"notifications-fragment\" class=\"fragment\">"
                "<p class=\"fragment-empty\">Application not found.</p>"
                "</div>"
            ),
            status_code=404,
            media_type="text/html",
        )

    logs = (
        db.query(models.NotificationLog)
        .filter(models.NotificationLog.application_id == application_id)
        .order_by(models.NotificationLog.created_at.desc())
        .all()
    )
    notifications = [
        {
            "id": l.id,
            "template_id": l.template_id,
            "channel": l.channel,
            "recipient": l.recipient or "",
            "status": l.status or "",
            "created_at": _iso(l.created_at),
            "sent_at": _iso(l.sent_at),
            "error": (l.error or "").strip(),
        }
        for l in logs
    ]
    return _fragment_response(
        request,
        "fragment_notifications",
        {"notifications": notifications, "application_id": application_id},
    )
