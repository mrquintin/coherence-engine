"""CRM bidirectional sync tests (prompt 55).

Covers:

* Backend signature verification (Affinity hex, HubSpot v3 base64).
* Backend ``parse_webhook`` -> :class:`CRMUpdate` shape.
* Outbound enqueue persists a ``crm_upsert_requested`` outbox event
  on Application status / Decision verdict change.
* Inbound :func:`apply_inbound_update`: merges tags / notes
  last-writer-wins, never mutates ``Decision.decision``, refuses to
  resolve unknown applications without raising.
* Reconciliation runs deterministically against a fixture diff and
  emits a ``crm_reconciliation_completed`` event.
* Router tests via TestClient: 401 on bad signature; 200 on a valid
  webhook with state mutation; 200 on a duplicate (no-op) webhook.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Sequence

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except BaseException as _exc:  # pragma: no cover - dependency missing
    pytest.skip(
        f"FastAPI unavailable in this interpreter: {_exc}",
        allow_module_level=True,
    )

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.routers.crm_webhooks import (
    reset_backends_for_tests,
    router as crm_webhook_router,
    set_affinity_backend_for_tests,
    set_hubspot_backend_for_tests,
    webhook_signature_ok,
)
from coherence_engine.server.fund.services.crm_backends import (
    AffinityBackend,
    CRMUpdate,
    HubSpotBackend,
    verify_affinity_webhook_signature,
    verify_hubspot_webhook_signature,
)
from coherence_engine.server.fund.services.crm_sync import (
    CRM_INBOUND_EVENT,
    CRM_OUTBOUND_EVENT,
    CRM_RECONCILIATION_EVENT,
    apply_inbound_update,
    enqueue_outbound_upsert,
    reconcile_crm_deltas,
)
from coherence_engine.server.fund.services.scheduled_jobs import (
    crm_daily_reconciliation,
)


AFFINITY_SECRET = "affinity-test-secret"
HUBSPOT_SECRET = "hubspot-test-secret"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _backend_reset():
    yield
    reset_backends_for_tests()


@pytest.fixture
def affinity_backend() -> AffinityBackend:
    return AffinityBackend(
        api_key="affinity-api-key",
        webhook_secret=AFFINITY_SECRET,
    )


@pytest.fixture
def hubspot_backend() -> HubSpotBackend:
    return HubSpotBackend(
        private_app_token="hs-private-token",
        webhook_secret=HUBSPOT_SECRET,
    )


def _seed_application(
    app_id: str = "app_crm_1",
    founder_id: str = "fnd_crm_1",
    status: str = "intake_created",
    email: str = "founder@example.com",
) -> models.Application:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id=founder_id,
            full_name="CRM Founder",
            email=email,
            country="US",
            company_name="CRMCo",
        )
        application = models.Application(
            id=app_id,
            founder_id=founder.id,
            one_liner="CRM pilot",
            requested_check_usd=75_000,
            use_of_funds_summary="seed",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status=status,
            scoring_mode="enforce",
        )
        db.add_all([founder, application])
        db.commit()
        db.refresh(application)
        return application
    finally:
        db.close()


def _seed_decision(
    application_id: str, *, verdict: str = "pass"
) -> models.Decision:
    db = SessionLocal()
    try:
        decision = models.Decision(
            id="dec_" + application_id,
            application_id=application_id,
            decision=verdict,
            policy_version="v1",
            parameter_set_id="default",
            threshold_required=0.5,
            coherence_observed=0.8,
            margin=0.3,
            failed_gates_json="[]",
        )
        db.add(decision)
        db.commit()
        db.refresh(decision)
        return decision
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backend signature verification (low-level)
# ---------------------------------------------------------------------------


def _affinity_sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _hubspot_sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def test_affinity_signature_accepts_valid_header():
    body = b'{"event":"deal_updated"}'
    header = _affinity_sign(AFFINITY_SECRET, body)
    assert verify_affinity_webhook_signature(
        AFFINITY_SECRET, body, header
    ) is True


def test_affinity_signature_accepts_sha256_prefix():
    body = b'{"event":"deal_updated"}'
    header = "sha256=" + _affinity_sign(AFFINITY_SECRET, body)
    assert verify_affinity_webhook_signature(
        AFFINITY_SECRET, body, header
    ) is True


def test_affinity_signature_rejects_bad_digest():
    body = b'{"event":"deal_updated"}'
    assert verify_affinity_webhook_signature(
        AFFINITY_SECRET, body, "deadbeef" * 8
    ) is False


def test_affinity_signature_rejects_empty_inputs():
    body = b"{}"
    assert verify_affinity_webhook_signature("", body, "sig") is False
    assert verify_affinity_webhook_signature(AFFINITY_SECRET, body, "") is False


def test_hubspot_signature_accepts_valid_header():
    body = b'[{"objectId":12345,"propertyName":"dealstage"}]'
    header = _hubspot_sign(HUBSPOT_SECRET, body)
    assert verify_hubspot_webhook_signature(
        HUBSPOT_SECRET, body, header
    ) is True


def test_hubspot_signature_rejects_bad_digest():
    body = b"{}"
    assert verify_hubspot_webhook_signature(
        HUBSPOT_SECRET, body, base64.b64encode(b"x" * 32).decode("ascii")
    ) is False


def test_hubspot_signature_rejects_empty_inputs():
    body = b"{}"
    assert verify_hubspot_webhook_signature("", body, "sig") is False
    assert verify_hubspot_webhook_signature(HUBSPOT_SECRET, body, "") is False


# ---------------------------------------------------------------------------
# Backend parse_webhook -> CRMUpdate
# ---------------------------------------------------------------------------


def test_affinity_parse_webhook_extracts_fields(affinity_backend):
    body = json.dumps(
        {
            "type": "list_entry.fields.update",
            "created_at": "2026-04-25T12:00:00Z",
            "body": {
                "opportunity_id": "opp_123",
                "fields": {
                    "application_id": "app_crm_1",
                    "founder_email": "founder@example.com",
                },
                "tags": ["hot", "follow_up"],
                "notes": ["partner left a note"],
                "stage": "due_diligence",
            },
        }
    ).encode("utf-8")
    update = affinity_backend.parse_webhook(body)
    assert update is not None
    assert update.provider == "affinity"
    assert update.external_id == "opp_123"
    assert update.application_id == "app_crm_1"
    assert list(update.tags) == ["hot", "follow_up"]
    assert list(update.notes) == ["partner left a note"]
    assert update.deal_stage == "due_diligence"


def test_hubspot_parse_webhook_extracts_fields(hubspot_backend):
    body = json.dumps(
        [
            {
                "objectId": 98765,
                "occurredAt": "2026-04-25T12:00:00Z",
                "properties": {
                    "application_id": "app_crm_1",
                    "email": "founder@example.com",
                    "tags": ["pipeline"],
                    "notes": ["check back next quarter"],
                    "dealstage": "decisionmaker_bought_in",
                },
            }
        ]
    ).encode("utf-8")
    update = hubspot_backend.parse_webhook(body)
    assert update is not None
    assert update.provider == "hubspot"
    assert update.external_id == "98765"
    assert update.application_id == "app_crm_1"
    assert list(update.tags) == ["pipeline"]
    assert update.deal_stage == "decisionmaker_bought_in"


def test_parse_webhook_handles_invalid_json(affinity_backend, hubspot_backend):
    assert affinity_backend.parse_webhook(b"not-json") is None
    assert hubspot_backend.parse_webhook(b"not-json") is None


# ---------------------------------------------------------------------------
# Outbound enqueue
# ---------------------------------------------------------------------------


def test_enqueue_outbound_upsert_writes_outbox_event():
    app = _seed_application()
    db = SessionLocal()
    try:
        result = enqueue_outbound_upsert(
            db, application_id=app.id, reason="status_change"
        )
        db.commit()
        assert "event_id" in result
        rows = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == CRM_OUTBOUND_EVENT)
            .all()
        )
        assert len(rows) == 1
        payload = json.loads(rows[0].payload_json)
        assert payload["application_id"] == app.id
        assert payload["status"] == "intake_created"
        assert payload["reason"] == "status_change"
    finally:
        db.close()


def test_enqueue_outbound_upsert_carries_verdict_when_decision_present():
    app = _seed_application()
    _seed_decision(app.id, verdict="pass")
    db = SessionLocal()
    try:
        enqueue_outbound_upsert(
            db, application_id=app.id, reason="verdict_change"
        )
        db.commit()
        row = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == CRM_OUTBOUND_EVENT)
            .one()
        )
        payload = json.loads(row.payload_json)
        assert payload["verdict"] == "pass"
    finally:
        db.close()


def test_enqueue_outbound_upsert_unknown_application_raises():
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            enqueue_outbound_upsert(
                db, application_id="missing", reason="status_change"
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Inbound apply_inbound_update -- conflict policy
# ---------------------------------------------------------------------------


def test_apply_inbound_update_merges_tags_last_writer_wins():
    app = _seed_application()
    update = CRMUpdate(
        provider="affinity",
        external_id="opp_xyz",
        application_id=app.id,
        tags=("hot", "lead"),
        notes=("first contact",),
        deal_stage="qualified",
        occurred_at="2026-04-25T10:00:00Z",
    )
    db = SessionLocal()
    try:
        outcome = apply_inbound_update(db, update)
        db.commit()
        assert outcome["applied"] is True
        assert outcome["application_id"] == app.id

        # Second event with new tags overwrites (last-writer-wins).
        replacement = CRMUpdate(
            provider="affinity",
            external_id="opp_xyz",
            application_id=app.id,
            tags=("cold",),
            notes=("not interested",),
            deal_stage="closed_lost",
            occurred_at="2026-04-25T11:00:00Z",
        )
        outcome2 = apply_inbound_update(db, replacement)
        db.commit()
        assert outcome2["applied"] is True

        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == CRM_INBOUND_EVENT)
            .order_by(models.EventOutbox.occurred_at.asc())
            .all()
        )
        assert len(events) == 2
        last_payload = json.loads(events[-1].payload_json)
        assert last_payload["tags"] == ["cold"]
        assert last_payload["deal_stage"] == "closed_lost"
    finally:
        db.close()


def test_apply_inbound_update_never_overwrites_decision_verdict():
    app = _seed_application()
    decision = _seed_decision(app.id, verdict="pass")
    update = CRMUpdate(
        provider="affinity",
        external_id="opp_xyz",
        application_id=app.id,
        tags=("hot",),
        deal_stage="closed_lost",  # CRM partner says closed_lost
    )
    db = SessionLocal()
    try:
        outcome = apply_inbound_update(db, update)
        db.commit()
        assert outcome["applied"] is True
        # Verdict must remain "pass" -- the partner stage label cannot
        # override a policy-produced decision.
        refreshed = (
            db.query(models.Decision)
            .filter(models.Decision.id == decision.id)
            .one()
        )
        assert refreshed.decision == "pass"

        # The persisted ledger payload also asserts the lock.
        evt = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == CRM_INBOUND_EVENT)
            .one()
        )
        payload = json.loads(evt.payload_json)
        assert payload.get("verdict_locked") is True
        assert "verdict" not in payload
    finally:
        db.close()


def test_apply_inbound_update_resolves_by_founder_email():
    app = _seed_application(email="lookup@example.com")
    update = CRMUpdate(
        provider="hubspot",
        external_id="deal_555",
        application_id="",
        founder_email="lookup@example.com",
        tags=("via_email",),
    )
    db = SessionLocal()
    try:
        outcome = apply_inbound_update(db, update)
        db.commit()
        assert outcome["applied"] is True
        assert outcome["application_id"] == app.id
    finally:
        db.close()


def test_apply_inbound_update_unresolved_application_no_op():
    update = CRMUpdate(
        provider="affinity",
        external_id="opp_unknown",
        application_id="app_does_not_exist",
        tags=("hot",),
    )
    db = SessionLocal()
    try:
        outcome = apply_inbound_update(db, update)
        db.commit()
        assert outcome["applied"] is False
        assert outcome["reason"] == "unresolved_application"
    finally:
        db.close()


def test_apply_inbound_update_idempotent_when_already_current():
    app = _seed_application()
    update = CRMUpdate(
        provider="affinity",
        external_id="opp_dup",
        application_id=app.id,
        tags=("dup",),
        deal_stage="qualified",
    )
    db = SessionLocal()
    try:
        first = apply_inbound_update(db, update)
        db.commit()
        assert first["applied"] is True
        second = apply_inbound_update(db, update)
        db.commit()
        assert second["applied"] is False
        assert second["reason"] == "already_current"
    finally:
        db.close()


def test_apply_inbound_update_does_not_clear_on_null():
    app = _seed_application()
    seeded = CRMUpdate(
        provider="affinity",
        external_id="opp_carry",
        application_id=app.id,
        tags=("keep_me",),
        deal_stage="qualified",
    )
    cleared = CRMUpdate(
        provider="affinity",
        external_id="opp_carry",
        application_id=app.id,
        tags=(),  # empty -> "no signal", not "clear"
        deal_stage="",
    )
    db = SessionLocal()
    try:
        apply_inbound_update(db, seeded)
        db.commit()
        outcome = apply_inbound_update(db, cleared)
        db.commit()
        # Carry-forward leaves the prior tags in place; therefore the
        # incoming "empty" delivery is no-op.
        assert outcome["applied"] is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class _StubBackend:
    """Stub backend whose ``fetch_recent_updates`` returns a fixture diff."""

    name: str
    diff: Sequence[CRMUpdate]

    def fetch_recent_updates(self, *, since_iso: str) -> Sequence[CRMUpdate]:
        assert since_iso  # contract: caller MUST pass a window
        return self.diff


def test_reconcile_crm_deltas_applies_missed_updates_and_emits_event():
    app = _seed_application()
    diff: List[CRMUpdate] = [
        CRMUpdate(
            provider="affinity",
            external_id="opp_a",
            application_id=app.id,
            tags=("missed",),
            deal_stage="qualified",
            occurred_at="2026-04-25T08:00:00Z",
        ),
        CRMUpdate(
            provider="affinity",
            external_id="opp_b",
            application_id="missing_app",
            tags=("orphan",),
            occurred_at="2026-04-25T08:30:00Z",
        ),
    ]
    backend = _StubBackend(name="affinity", diff=diff)
    db = SessionLocal()
    try:
        result = reconcile_crm_deltas(
            db, backend, now=datetime(2026, 4, 25, 9, 0, tzinfo=timezone.utc)
        )
        db.commit()
        assert result.applied == 1
        assert result.unresolved == 1
        assert result.skipped_already_applied == 0

        evt = (
            db.query(models.EventOutbox)
            .filter(
                models.EventOutbox.event_type == CRM_RECONCILIATION_EVENT
            )
            .one()
        )
        payload = json.loads(evt.payload_json)
        assert payload["provider"] == "affinity"
        assert payload["applied"] == 1
        assert payload["unresolved"] == 1
        assert payload["window_started_at"] == "2026-04-24T09:00:00Z"
        assert payload["window_ended_at"] == "2026-04-25T09:00:00Z"
    finally:
        db.close()


def test_reconcile_crm_deltas_deterministic_against_fixture():
    app = _seed_application()
    delta = CRMUpdate(
        provider="affinity",
        external_id="opp_det",
        application_id=app.id,
        tags=("deterministic",),
        deal_stage="qualified",
        occurred_at="2026-04-25T08:00:00Z",
    )
    backend = _StubBackend(name="affinity", diff=[delta])
    fixed_now = datetime(2026, 4, 25, 9, 0, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        first = reconcile_crm_deltas(db, backend, now=fixed_now)
        db.commit()
        # Second run with the same diff is idempotent: no new applies.
        second = reconcile_crm_deltas(db, backend, now=fixed_now)
        db.commit()
        assert first.applied == 1
        assert second.applied == 0
        assert second.skipped_already_applied == 1

        events = (
            db.query(models.EventOutbox)
            .filter(
                models.EventOutbox.event_type == CRM_RECONCILIATION_EVENT
            )
            .order_by(models.EventOutbox.occurred_at.asc())
            .all()
        )
        assert len(events) == 2
    finally:
        db.close()


def test_crm_daily_reconciliation_emits_one_event_per_backend():
    app = _seed_application()
    delta_a = CRMUpdate(
        provider="affinity",
        external_id="opp_a",
        application_id=app.id,
        tags=("via_affinity",),
        deal_stage="qualified",
    )
    delta_h = CRMUpdate(
        provider="hubspot",
        external_id="deal_h",
        application_id=app.id,
        tags=("via_hubspot",),
        deal_stage="qualified",
    )
    affinity = _StubBackend(name="affinity", diff=[delta_a])
    hubspot = _StubBackend(name="hubspot", diff=[delta_h])
    db = SessionLocal()
    try:
        result = crm_daily_reconciliation(
            db,
            backends=[affinity, hubspot],
            now=datetime(2026, 4, 25, 7, 0, tzinfo=timezone.utc),
        )
        db.commit()
        providers = [b["provider"] for b in result["backends"]]
        assert providers == ["affinity", "hubspot"]
        events = (
            db.query(models.EventOutbox)
            .filter(
                models.EventOutbox.event_type == CRM_RECONCILIATION_EVENT
            )
            .all()
        )
        assert len(events) == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router tests via TestClient
# ---------------------------------------------------------------------------


def _build_client() -> TestClient:
    app = FastAPI()
    app.include_router(crm_webhook_router)
    return TestClient(app)


def test_affinity_webhook_rejects_bad_signature(affinity_backend):
    set_affinity_backend_for_tests(affinity_backend)
    client = _build_client()
    body = b'{"body":{"opportunity_id":"opp_1"}}'
    response = client.post(
        "/webhooks/crm/affinity",
        content=body,
        headers={"Affinity-Webhook-Signature": "deadbeef"},
    )
    assert response.status_code == 401


def test_affinity_webhook_accepts_valid_signature(affinity_backend):
    app = _seed_application()
    set_affinity_backend_for_tests(affinity_backend)
    client = _build_client()
    body = json.dumps(
        {
            "body": {
                "opportunity_id": "opp_aff_1",
                "fields": {"application_id": app.id},
                "tags": ["hot"],
                "stage": "qualified",
            }
        }
    ).encode("utf-8")
    sig = _affinity_sign(AFFINITY_SECRET, body)
    response = client.post(
        "/webhooks/crm/affinity",
        content=body,
        headers={"Affinity-Webhook-Signature": sig},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["applied"] is True
    assert data["application_id"] == app.id


def test_affinity_webhook_duplicate_is_no_op(affinity_backend):
    app = _seed_application()
    set_affinity_backend_for_tests(affinity_backend)
    client = _build_client()
    body = json.dumps(
        {
            "body": {
                "opportunity_id": "opp_dup",
                "fields": {"application_id": app.id},
                "tags": ["x"],
                "stage": "qualified",
            }
        }
    ).encode("utf-8")
    sig = _affinity_sign(AFFINITY_SECRET, body)
    headers = {"Affinity-Webhook-Signature": sig}
    first = client.post(
        "/webhooks/crm/affinity", content=body, headers=headers
    )
    second = client.post(
        "/webhooks/crm/affinity", content=body, headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["applied"] is True
    assert second.json()["data"]["applied"] is False
    assert second.json()["data"]["reason"] == "already_current"


def test_hubspot_webhook_rejects_bad_signature(hubspot_backend):
    set_hubspot_backend_for_tests(hubspot_backend)
    client = _build_client()
    body = b'[{"objectId":1}]'
    response = client.post(
        "/webhooks/crm/hubspot",
        content=body,
        headers={"X-HubSpot-Signature-v3": "not-base64"},
    )
    assert response.status_code == 401


def test_hubspot_webhook_accepts_valid_signature(hubspot_backend):
    app = _seed_application()
    set_hubspot_backend_for_tests(hubspot_backend)
    client = _build_client()
    body = json.dumps(
        [
            {
                "objectId": 9001,
                "properties": {
                    "application_id": app.id,
                    "tags": ["pipeline"],
                    "dealstage": "qualified",
                },
            }
        ]
    ).encode("utf-8")
    sig = _hubspot_sign(HUBSPOT_SECRET, body)
    response = client.post(
        "/webhooks/crm/hubspot",
        content=body,
        headers={"X-HubSpot-Signature-v3": sig},
    )
    assert response.status_code == 200
    assert response.json()["data"]["applied"] is True


def test_webhook_signature_ok_helper_round_trips(affinity_backend):
    body = b'{"body":{"opportunity_id":"opp_1"}}'
    sig = _affinity_sign(AFFINITY_SECRET, body)
    assert webhook_signature_ok(
        affinity_backend, body, {"Affinity-Webhook-Signature": sig}
    ) is True
    assert webhook_signature_ok(
        affinity_backend, body, {"Affinity-Webhook-Signature": "bad"}
    ) is False
