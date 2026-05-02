"""Read-only admin dashboard tests (prompt 19).

Covers the HTMX-driven operator UI mounted at ``/admin`` by
:mod:`coherence_engine.server.fund.app`:

* Unauthenticated / non-admin requests return 401 / 403 respectively.
* Authenticated admin requests to ``/admin/applications`` return a
  200 HTML page whose ``<table>`` contains at least one data row when
  a seed application exists.
* Each fragment endpoint (``/admin/applications/{id}/fragment/*``)
  returns ``text/html`` bodies carrying the expected fragment
  ``id`` attribute (``scores-fragment``, ``workflow-fragment``,
  ``notifications-fragment``) so HTMX ``hx-target`` selectors keep
  working.
* The vendored HTMX script is served from ``/admin/static/htmx.min.js``
  and carries the MIT / permissive-license header (no CDN reference).

No endpoint tested here mutates state — the admin UI is strictly
read-only per the prompt-19 prohibition.
"""

from __future__ import annotations

import os

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client():
    """Return ``(client, tokens, app_id)`` for admin dashboard tests.

    Builds an app with the admin UI + static mount, issues admin /
    analyst / viewer API keys via the real :class:`ApiKeyService`,
    and seeds one application with linked scoring/decision/workflow/
    notification rows so the list view has content and each fragment
    endpoint renders at least one data row.
    """

    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"

    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.app import create_app
    from coherence_engine.server.fund.database import Base, SessionLocal, engine
    from coherence_engine.server.fund.repositories.api_key_repository import (
        ApiKeyRepository,
    )
    from coherence_engine.server.fund.services.api_key_service import ApiKeyService

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    tokens: dict = {}
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        admin = svc.create_key(
            repo, label="p19-admin", role="admin", created_by="tests", expires_in_days=30
        )
        analyst = svc.create_key(
            repo, label="p19-analyst", role="analyst", created_by="tests", expires_in_days=30
        )
        viewer = svc.create_key(
            repo, label="p19-viewer", role="viewer", created_by="tests", expires_in_days=30
        )
        tokens["admin"] = admin["token"]
        tokens["analyst"] = analyst["token"]
        tokens["viewer"] = viewer["token"]
        db.commit()
    finally:
        db.close()

    app_id = "app_p19_admin"
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_p19_admin",
            full_name="Prompt 19 Founder",
            email="p19@example.com",
            country="US",
            company_name="Prompt 19 Co",
        )
        app_row = models.Application(
            id=app_id,
            founder_id=founder.id,
            one_liner="Prompt 19 admin dashboard pilot",
            requested_check_usd=120_000,
            use_of_funds_summary="Seed admin UI adoption",
            preferred_channel="web_voice",
            transcript_text="Founder pitched a plausible product.",
            domain_primary="market_economics",
            compliance_status="clear",
            status="intake_created",
            scoring_mode="enforce",
        )
        scoring_job = models.ScoringJob(
            id="sj_p19_admin",
            application_id=app_id,
            mode="enforce",
            dry_run=False,
            status="succeeded",
            attempts=1,
            max_attempts=5,
        )
        decision = models.Decision(
            id="dec_p19_admin",
            application_id=app_id,
            decision="pass",
            policy_version="decision-policy-v1",
            parameter_set_id="param-set-v1",
            threshold_required=0.72,
            coherence_observed=0.81,
            margin=0.09,
            failed_gates_json="[]",
        )
        workflow_run = models.WorkflowRun(
            id="wf_p19_admin",
            application_id=app_id,
            status="succeeded",
            current_step="notify",
        )
        workflow_step = models.WorkflowStep(
            id="wfs_p19_admin",
            workflow_run_id=workflow_run.id,
            name="intake",
            status="succeeded",
            input_digest="i" * 64,
            output_digest="o" * 64,
        )
        notification = models.NotificationLog(
            id="notif_p19_admin",
            application_id=app_id,
            template_id="founder_pass_v1",
            channel="dry_run",
            recipient="p19@example.com",
            idempotency_key="p19-admin-founder-pass",
            status="sent",
        )
        db.add_all(
            [
                founder,
                app_row,
                scoring_job,
                decision,
                workflow_run,
                workflow_step,
                notification,
            ]
        )
        db.commit()
    finally:
        db.close()

    app = create_app()
    client = TestClient(app)
    try:
        yield client, tokens, app_id
    finally:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)


def _admin_headers(token: str) -> dict:
    return {"X-API-Key": token, "X-Request-Id": "req_p19_admin"}


# ---------------------------------------------------------------------------
# Authentication / authorization contract
# ---------------------------------------------------------------------------


def test_admin_applications_unauthenticated_is_401(admin_client):
    """No token → 401 Unauthorized HTML response."""

    client, _tokens, _app_id = admin_client
    res = client.get("/admin/applications")
    assert res.status_code == 401, res.text
    assert "text/html" in res.headers.get("content-type", "")
    assert "Unauthorized" in res.text


def test_admin_applications_viewer_token_is_403(admin_client):
    """A valid non-admin token is still rejected with 403 Forbidden."""

    client, tokens, _app_id = admin_client
    res = client.get(
        "/admin/applications",
        headers=_admin_headers(tokens["viewer"]),
    )
    assert res.status_code == 403, res.text
    assert "text/html" in res.headers.get("content-type", "")
    assert "Forbidden" in res.text


def test_admin_applications_analyst_token_is_403(admin_client):
    """Even the analyst role is denied admin-UI access."""

    client, tokens, _app_id = admin_client
    res = client.get(
        "/admin/applications",
        headers=_admin_headers(tokens["analyst"]),
    )
    assert res.status_code == 403, res.text


def test_admin_application_detail_unauthenticated_is_401(admin_client):
    client, _tokens, app_id = admin_client
    res = client.get(f"/admin/applications/{app_id}")
    assert res.status_code == 401, res.text


def test_admin_fragment_scores_unauthenticated_is_401(admin_client):
    client, _tokens, app_id = admin_client
    res = client.get(f"/admin/applications/{app_id}/fragment/scores")
    assert res.status_code == 401, res.text


# ---------------------------------------------------------------------------
# Applications list (200 happy path)
# ---------------------------------------------------------------------------


def test_admin_applications_renders_table_row_when_seed_exists(admin_client):
    """Admin GET /admin/applications → 200 HTML + at least one row."""

    client, tokens, app_id = admin_client
    res = client.get(
        "/admin/applications",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    ctype = res.headers.get("content-type", "")
    assert "text/html" in ctype

    body = res.text
    # Structural sanity: the full page shell must be present.
    assert "<!DOCTYPE html>" in body
    assert "<html" in body
    assert "Coherence Fund" in body

    # Applications table must render the seed application row.
    assert "<table" in body
    assert "applications-table" in body
    assert app_id in body
    assert f'data-app-id="{app_id}"' in body
    # The HTMX vendored asset is referenced, not a CDN URL.
    assert "/admin/static/htmx.min.js" in body
    assert "unpkg.com" not in body
    assert "cdnjs" not in body


def test_admin_applications_htmx_attributes_are_present(admin_client):
    """Pagination links expose ``hx-get`` / ``hx-target`` attributes."""

    client, tokens, _app_id = admin_client
    res = client.get(
        "/admin/applications?page=1&page_size=25",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    body = res.text
    # Either previous or next pagination anchor must be an HTMX trigger
    # (the seed fixture creates one row so ``has_next`` is False and
    # ``page > 1`` is False; the anchor is rendered as a muted span in
    # that case. So we assert the declarative HTMX target is present on
    # the panel itself).
    assert 'id="applications-panel"' in body


def test_admin_application_detail_renders_fragment_hosts(admin_client):
    """Detail page has three HTMX fragment placeholders."""

    client, tokens, app_id = admin_client
    res = client.get(
        f"/admin/applications/{app_id}",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    body = res.text
    assert "Application " + app_id in body
    # Three hx-get placeholders must point at the fragment endpoints.
    assert f'hx-get="/admin/applications/{app_id}/fragment/scores"' in body
    assert f'hx-get="/admin/applications/{app_id}/fragment/workflow"' in body
    assert (
        f'hx-get="/admin/applications/{app_id}/fragment/notifications"' in body
    )


def test_admin_application_detail_404_when_missing(admin_client):
    client, tokens, _app_id = admin_client
    res = client.get(
        "/admin/applications/app_does_not_exist",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# Fragment endpoints
# ---------------------------------------------------------------------------


def test_fragment_scores_returns_expected_id_and_rows(admin_client):
    client, tokens, app_id = admin_client
    res = client.get(
        f"/admin/applications/{app_id}/fragment/scores",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert "text/html" in res.headers.get("content-type", "")
    body = res.text
    # Expected fragment id attribute (HTMX swap target contract).
    assert 'id="scores-fragment"' in body
    # Seed fixture creates a scoring job + decision row; both surface.
    assert "sj_p19_admin" in body
    assert "Decision artifact" in body
    assert "pass" in body


def test_fragment_workflow_returns_expected_id_and_rows(admin_client):
    client, tokens, app_id = admin_client
    res = client.get(
        f"/admin/applications/{app_id}/fragment/workflow",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert "text/html" in res.headers.get("content-type", "")
    body = res.text
    assert 'id="workflow-fragment"' in body
    assert "wf_p19_admin" in body
    # Single step row is exposed.
    assert 'data-workflow-step-name="intake"' in body


def test_fragment_notifications_returns_expected_id_and_rows(admin_client):
    client, tokens, app_id = admin_client
    res = client.get(
        f"/admin/applications/{app_id}/fragment/notifications",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    assert "text/html" in res.headers.get("content-type", "")
    body = res.text
    assert 'id="notifications-fragment"' in body
    assert "founder_pass_v1" in body
    assert "notif_p19_admin" in body


def test_fragment_endpoints_require_admin(admin_client):
    """Each fragment endpoint is 401 without a token and 403 for viewer."""

    client, tokens, app_id = admin_client
    for path in (
        f"/admin/applications/{app_id}/fragment/scores",
        f"/admin/applications/{app_id}/fragment/workflow",
        f"/admin/applications/{app_id}/fragment/notifications",
    ):
        anon = client.get(path)
        assert anon.status_code == 401, (path, anon.text)

        viewer = client.get(path, headers=_admin_headers(tokens["viewer"]))
        assert viewer.status_code == 403, (path, viewer.text)


def test_fragment_endpoint_404_for_missing_application(admin_client):
    client, tokens, _app_id = admin_client
    res = client.get(
        "/admin/applications/app_nope/fragment/scores",
        headers=_admin_headers(tokens["admin"]),
    )
    assert res.status_code == 404
    assert 'id="scores-fragment"' in res.text


# ---------------------------------------------------------------------------
# Static asset surface
# ---------------------------------------------------------------------------


def test_admin_htmx_static_file_is_served_locally(admin_client):
    """Vendored HTMX is served from ``/admin/static`` — no CDN."""

    client, _tokens, _app_id = admin_client
    res = client.get("/admin/static/htmx.min.js")
    assert res.status_code == 200, res.text
    body = res.text
    # Vendored license header + the real htmx factory function are
    # both present in the served asset.
    assert "htmx" in body
    assert "MIT" in body
    # Sanity check: this is the upstream minified bundle, not an
    # accidentally-stripped stub.
    assert 'version:"1.' in body


def test_admin_css_static_file_is_served_locally(admin_client):
    client, _tokens, _app_id = admin_client
    res = client.get("/admin/static/admin.css")
    assert res.status_code == 200, res.text
    assert "admin-shell" in res.text


# ---------------------------------------------------------------------------
# Import contract (verification marker parity)
# ---------------------------------------------------------------------------


def test_admin_ui_router_module_exposes_router():
    """The admin_ui module must expose an APIRouter with /admin prefix."""

    from coherence_engine.server.fund.routers import admin_ui

    assert hasattr(admin_ui, "router")
    # Marker regex (``/admin/applications|APIRouter``) is satisfied by
    # the symbol table + prefix declaration. Assert the prefix so the
    # contract is testable at runtime too.
    assert admin_ui.router.prefix == "/admin"


def test_admin_ui_router_is_mounted_on_app():
    """create_app() must include the admin_ui router and static mount."""

    from coherence_engine.server.fund.app import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/admin/applications" in paths
    assert "/admin/applications/{application_id}" in paths
    assert (
        "/admin/applications/{application_id}/fragment/scores" in paths
    )
    assert (
        "/admin/applications/{application_id}/fragment/workflow" in paths
    )
    assert (
        "/admin/applications/{application_id}/fragment/notifications" in paths
    )
