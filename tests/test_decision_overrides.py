"""Decision override service + partner_api router tests (prompt 35).

Covers:

* :class:`DecisionOverrideService` validation rules — reason_code enum,
  reason_text length floor, pass→reject memo requirement.
* Idempotency: a second override write without ``unrevise=True``
  returns the existing row and does NOT emit a duplicate event.
* The ``--unrevise`` path supersedes the prior row and emits a fresh
  ``decision_overridden.v1`` event.
* RBAC enforcement on ``/partner/applications/{id}/override``: viewer
  → 403, partner → 201, admin → 201.
* Audit log emission via :func:`audit_log` (write to
  :class:`ApiKeyAuditEvent`).

The module mirrors the fixture layout in ``test_admin_dashboard.py`` so
the same DB / API-key wiring is reused.
"""

from __future__ import annotations

import json
import os

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def partner_client():
    """Return ``(client, tokens, app_id, db_session_factory)`` for partner tests."""

    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    os.environ["COHERENCE_FUND_RATE_LIMIT_ENABLED"] = "false"

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
            repo,
            label="p35-admin",
            role="admin",
            created_by="tests",
            expires_in_days=30,
        )
        partner = svc.create_key(
            repo,
            label="p35-partner",
            role="partner",
            created_by="tests",
            expires_in_days=30,
        )
        viewer = svc.create_key(
            repo,
            label="p35-viewer",
            role="viewer",
            created_by="tests",
            expires_in_days=30,
        )
        tokens["admin"] = admin["token"]
        tokens["partner"] = partner["token"]
        tokens["viewer"] = viewer["token"]
        db.commit()
    finally:
        db.close()

    app_id = "app_p35_partner"
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_p35_partner",
            full_name="Prompt 35 Founder",
            email="p35@example.com",
            country="US",
            company_name="Prompt 35 Co",
        )
        application = models.Application(
            id=app_id,
            founder_id=founder.id,
            one_liner="Prompt 35 partner override pilot",
            requested_check_usd=200_000,
            use_of_funds_summary="Seed partner override flow",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="decision_issued",
            scoring_mode="enforce",
        )
        decision = models.Decision(
            id="dec_p35_partner",
            application_id=app_id,
            decision="pass",
            policy_version="decision-policy-v1",
            parameter_set_id="param-set-v1",
            threshold_required=0.72,
            coherence_observed=0.81,
            margin=0.09,
            failed_gates_json="[]",
        )
        db.add_all([founder, application, decision])
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


def _hdr(token: str, request_id: str = "req_p35") -> dict:
    return {"X-API-Key": token, "X-Request-Id": request_id}


_GOOD_REASON_TEXT = (
    "After reviewing the founder's brokerage statement and "
    "follow-up call, the policy gate fired on a stale data point."
)


# ---------------------------------------------------------------------------
# Service-level validation
# ---------------------------------------------------------------------------


def test_service_rejects_unknown_reason_code(partner_client):
    _client, _tokens, app_id = partner_client
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.decision_overrides import (
        DecisionOverrideService,
        OverrideError,
    )

    db = SessionLocal()
    try:
        svc = DecisionOverrideService(db)
        with pytest.raises(OverrideError) as exc:
            svc.create_override(
                application_id=app_id,
                override_verdict="reject",
                reason_code="not_a_real_code",
                reason_text=_GOOD_REASON_TEXT,
                overridden_by="tests",
                justification_uri="s3://memos/x.pdf",
            )
        assert exc.value.code == "INVALID_REASON_CODE"
    finally:
        db.close()


def test_service_rejects_short_reason_text(partner_client):
    _client, _tokens, app_id = partner_client
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.decision_overrides import (
        DecisionOverrideService,
        OverrideError,
    )

    db = SessionLocal()
    try:
        svc = DecisionOverrideService(db)
        with pytest.raises(OverrideError) as exc:
            svc.create_override(
                application_id=app_id,
                override_verdict="reject",
                reason_code="factual_error",
                reason_text="too short",
                overridden_by="tests",
                justification_uri="s3://memos/x.pdf",
            )
        assert exc.value.code == "REASON_TEXT_TOO_SHORT"
    finally:
        db.close()


def test_service_requires_memo_for_pass_to_reject(partner_client):
    _client, _tokens, app_id = partner_client
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.decision_overrides import (
        DecisionOverrideService,
        OverrideError,
    )

    db = SessionLocal()
    try:
        svc = DecisionOverrideService(db)
        with pytest.raises(OverrideError) as exc:
            svc.create_override(
                application_id=app_id,
                override_verdict="reject",
                reason_code="factual_error",
                reason_text=_GOOD_REASON_TEXT,
                overridden_by="tests",
                justification_uri=None,
            )
        assert exc.value.code == "MEMO_REQUIRED"
    finally:
        db.close()


def test_service_idempotent_on_duplicate_call(partner_client):
    _client, _tokens, app_id = partner_client
    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.decision_overrides import (
        DecisionOverrideService,
    )

    db = SessionLocal()
    try:
        svc = DecisionOverrideService(db)
        first = svc.create_override(
            application_id=app_id,
            override_verdict="reject",
            reason_code="factual_error",
            reason_text=_GOOD_REASON_TEXT,
            overridden_by="tests",
            justification_uri="s3://memos/x.pdf",
        )
        db.commit()
        assert first.created is True

        second = svc.create_override(
            application_id=app_id,
            override_verdict="reject",
            reason_code="policy_misalignment",
            reason_text=_GOOD_REASON_TEXT + " (second attempt)",
            overridden_by="tests",
            justification_uri="s3://memos/y.pdf",
        )
        db.commit()
        assert second.created is False
        assert second.override.id == first.override.id

        # Outbox should carry exactly ONE override event for this app.
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "decision_overridden")
            .all()
        )
        assert len(events) == 1, "duplicate override write must not re-emit event"
    finally:
        db.close()


def test_service_unrevise_supersedes_prior_row_and_emits_event(partner_client):
    _client, _tokens, app_id = partner_client
    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.decision_overrides import (
        DecisionOverrideService,
    )

    db = SessionLocal()
    try:
        svc = DecisionOverrideService(db)
        first = svc.create_override(
            application_id=app_id,
            override_verdict="reject",
            reason_code="factual_error",
            reason_text=_GOOD_REASON_TEXT,
            overridden_by="tests",
            justification_uri="s3://memos/x.pdf",
        )
        db.commit()
        assert first.created is True

        second = svc.create_override(
            application_id=app_id,
            override_verdict="manual_review",
            reason_code="manual_diligence",
            reason_text=_GOOD_REASON_TEXT + " (revised)",
            overridden_by="tests",
            unrevise=True,
        )
        db.commit()
        assert second.created is True
        assert second.superseded_id == first.override.id

        prior = (
            db.query(models.DecisionOverride)
            .filter(models.DecisionOverride.id == first.override.id)
            .one()
        )
        assert prior.status == "superseded"

        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "decision_overridden")
            .all()
        )
        assert len(events) == 2

        latest = max(events, key=lambda e: e.occurred_at)
        latest_payload = json.loads(latest.payload_json)
        assert latest_payload["override_id"] == second.override.id
        assert latest_payload["superseded_override_id"] == first.override.id
        assert latest_payload["override_verdict"] == "manual_review"
    finally:
        db.close()


def test_service_emits_decision_overridden_event_payload_shape(partner_client):
    _client, _tokens, app_id = partner_client
    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.database import SessionLocal
    from coherence_engine.server.fund.services.decision_overrides import (
        DecisionOverrideService,
    )

    db = SessionLocal()
    try:
        svc = DecisionOverrideService(db)
        result = svc.create_override(
            application_id=app_id,
            override_verdict="reject",
            reason_code="regulatory_constraint",
            reason_text=_GOOD_REASON_TEXT,
            overridden_by="partner@fund.test",
            justification_uri="s3://memos/r.pdf",
        )
        db.commit()
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "decision_overridden")
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        for key in (
            "override_id",
            "application_id",
            "original_verdict",
            "override_verdict",
            "reason_code",
            "overridden_by",
            "justification_uri",
            "overridden_at",
        ):
            assert key in payload, f"missing event field: {key}"
        assert payload["application_id"] == app_id
        assert payload["override_id"] == result.override.id
        assert payload["original_verdict"] == "pass"
        assert payload["override_verdict"] == "reject"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router-level RBAC (require_role)
# ---------------------------------------------------------------------------


def test_router_unauthenticated_is_401(partner_client):
    client, _tokens, app_id = partner_client
    res = client.post(
        f"/partner/applications/{app_id}/override",
        json={
            "override_verdict": "reject",
            "reason_code": "factual_error",
            "reason_text": _GOOD_REASON_TEXT,
            "justification_uri": "s3://memos/x.pdf",
        },
    )
    assert res.status_code == 401, res.text


def test_router_viewer_role_is_403(partner_client):
    client, tokens, app_id = partner_client
    res = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["viewer"]),
        json={
            "override_verdict": "reject",
            "reason_code": "factual_error",
            "reason_text": _GOOD_REASON_TEXT,
            "justification_uri": "s3://memos/x.pdf",
        },
    )
    assert res.status_code == 403, res.text


def test_router_partner_role_can_override(partner_client):
    client, tokens, app_id = partner_client
    res = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["partner"]),
        json={
            "override_verdict": "reject",
            "reason_code": "factual_error",
            "reason_text": _GOOD_REASON_TEXT,
            "justification_uri": "s3://memos/x.pdf",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["error"] is None
    assert body["data"]["override_verdict"] == "reject"
    assert body["data"]["original_verdict"] == "pass"
    assert body["data"]["reason_code"] == "factual_error"
    assert body["data"]["created"] is True


def test_router_admin_role_can_override(partner_client):
    client, tokens, app_id = partner_client
    res = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["admin"]),
        json={
            "override_verdict": "manual_review",
            "reason_code": "manual_diligence",
            "reason_text": _GOOD_REASON_TEXT,
        },
    )
    assert res.status_code == 201, res.text


def test_router_rejects_short_reason_text(partner_client):
    client, tokens, app_id = partner_client
    res = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["partner"]),
        json={
            "override_verdict": "reject",
            "reason_code": "factual_error",
            "reason_text": "nope",
            "justification_uri": "s3://memos/x.pdf",
        },
    )
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "REASON_TEXT_TOO_SHORT"


def test_router_idempotent_second_call_returns_200(partner_client):
    client, tokens, app_id = partner_client
    payload = {
        "override_verdict": "reject",
        "reason_code": "factual_error",
        "reason_text": _GOOD_REASON_TEXT,
        "justification_uri": "s3://memos/x.pdf",
    }
    first = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["partner"]),
        json=payload,
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["partner"]),
        json=payload,
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["created"] is False
    assert (
        first.json()["data"]["override_id"]
        == second.json()["data"]["override_id"]
    )


def test_router_pipeline_partner_role_returns_filtered_data(partner_client):
    client, tokens, app_id = partner_client
    res = client.get(
        "/partner/pipeline?domain=market_economics",
        headers=_hdr(tokens["partner"]),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["error"] is None
    assert body["data"]["filter"]["domain"] == "market_economics"
    items = body["data"]["items"]
    assert any(i["application_id"] == app_id for i in items)


def test_router_audit_log_emitted_on_override(partner_client):
    client, tokens, app_id = partner_client
    res = client.post(
        f"/partner/applications/{app_id}/override",
        headers=_hdr(tokens["partner"]),
        json={
            "override_verdict": "reject",
            "reason_code": "factual_error",
            "reason_text": _GOOD_REASON_TEXT,
            "justification_uri": "s3://memos/x.pdf",
        },
    )
    assert res.status_code == 201, res.text

    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.database import SessionLocal

    db = SessionLocal()
    try:
        events = (
            db.query(models.ApiKeyAuditEvent)
            .filter(models.ApiKeyAuditEvent.action == "decision_override_applied")
            .all()
        )
        assert len(events) >= 1
        details = json.loads(events[0].details_json or "{}")
        assert details.get("application_id") == app_id
        assert details.get("override_verdict") == "reject"
    finally:
        db.close()
