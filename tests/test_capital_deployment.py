"""Capital deployment service + router tests (prompt 51).

Covers:

* Backend prepare/execute on the in-tree synthetic Stripe and bank
  paths (no real HTTP -- Mercury and Stripe are mocked at the
  ``backend_for_method`` factory level).
* Service-layer state machine: prepare -> approve -> execute, with
  idempotent prepare and the dual-approval gate at >=
  :data:`DUAL_APPROVAL_THRESHOLD_USD`.
* Router: 409 when approving without prior prepare, 403 when
  executing without approve, 401 when no API token presented,
  Stripe webhook signature verification.

The tests deliberately do NOT make any real network calls -- both
backends are stubbed via :func:`set_backend_factory_for_tests`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

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
from coherence_engine.server.fund.routers.capital import (
    reset_backend_factory_for_tests,
    router as capital_router,
    set_backend_factory_for_tests,
)
from coherence_engine.server.fund.services.capital_backends import (
    BankTransferBackend,
    CapitalBackendError,
    StripeConnectBackend,
    verify_stripe_webhook_signature,
)
from coherence_engine.server.fund.services.capital_deployment import (
    CapitalDeployment,
    CapitalDeploymentError,
    DUAL_APPROVAL_THRESHOLD_USD,
    InstructionStateError,
    compute_idempotency_key,
)


STRIPE_WEBHOOK_SECRET = "stripe-test-webhook-secret"


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
def _stripe_env():
    prior = os.environ.get("STRIPE_WEBHOOK_SECRET")
    os.environ["STRIPE_WEBHOOK_SECRET"] = STRIPE_WEBHOOK_SECRET
    yield
    if prior is None:
        del os.environ["STRIPE_WEBHOOK_SECRET"]
    else:
        os.environ["STRIPE_WEBHOOK_SECRET"] = prior


@pytest.fixture
def stripe_backend():
    return StripeConnectBackend(
        api_key="sk_test_dummy",
        connect_account_id="acct_test",
        webhook_secret=STRIPE_WEBHOOK_SECRET,
    )


@pytest.fixture
def bank_backend():
    return BankTransferBackend(api_token="merc-test-token")


def _persist_application(app_id: str = "app_capital_1") -> tuple[models.Founder, models.Application]:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_capital_1",
            full_name="Capital Founder",
            email="cap@example.com",
            country="US",
            company_name="Capital Co",
        )
        application = models.Application(
            id=app_id,
            founder_id=founder.id,
            one_liner="Capital deployment pilot",
            requested_check_usd=100_000,
            use_of_funds_summary="Seed",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="decision_issued",
            scoring_mode="enforce",
        )
        db.add_all([founder, application])
        db.commit()
        db.refresh(founder)
        db.refresh(application)
        return founder, application
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backend unit tests
# ---------------------------------------------------------------------------


def test_stripe_prepare_returns_intent_ref(stripe_backend):
    class _Stub:
        idempotency_key = "abc"
        target_account_ref = "acct_x"
        provider_intent_ref = ""

    res = stripe_backend.prepare(instruction=_Stub())
    assert res.provider_intent_ref.startswith("tr_intent_")


def test_stripe_execute_requires_prior_prepare(stripe_backend):
    class _Stub:
        idempotency_key = "abc"
        target_account_ref = "acct_x"
        provider_intent_ref = ""

    with pytest.raises(CapitalBackendError):
        stripe_backend.execute(instruction=_Stub())


def test_bank_prepare_rejects_unknown_counterparty(bank_backend):
    class _Stub:
        idempotency_key = "abc"
        target_account_ref = "raw_account_routing_data"
        provider_intent_ref = ""

    with pytest.raises(CapitalBackendError):
        bank_backend.prepare(instruction=_Stub())


def test_bank_prepare_accepts_counterparty_token(bank_backend):
    class _Stub:
        idempotency_key = "abc"
        target_account_ref = "cp_mercury_xyz"
        provider_intent_ref = ""

    res = bank_backend.prepare(instruction=_Stub())
    assert res.provider_intent_ref.startswith("pmt_intent_")


# ---------------------------------------------------------------------------
# Service-level state machine
# ---------------------------------------------------------------------------


def test_service_prepare_creates_instruction_and_event(stripe_backend):
    _founder, application = _persist_application()
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        instruction = service.prepare(
            backend=stripe_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=50_000,
            target_account_ref="acct_test",
            preparation_method="stripe",
            prepared_by="partner:alice",
        )
        db.commit()
        assert instruction.status == "prepared"
        assert instruction.provider_intent_ref.startswith("tr_intent_")
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investment_funding_prepared")
            .all()
        )
        assert len(events) == 1
    finally:
        db.close()


def test_service_prepare_is_idempotent(stripe_backend):
    _founder, application = _persist_application()
    key = compute_idempotency_key(application.id, "stripe", salt="req-1")
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        first = service.prepare(
            backend=stripe_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=50_000,
            target_account_ref="acct_test",
            preparation_method="stripe",
            prepared_by="partner:alice",
            idempotency_key=key,
        )
        db.commit()
        second = service.prepare(
            backend=stripe_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=50_000,
            target_account_ref="acct_test",
            preparation_method="stripe",
            prepared_by="partner:alice",
            idempotency_key=key,
        )
        db.commit()
        assert first.id == second.id
        # Only one event was emitted on the first prepare.
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investment_funding_prepared")
            .count()
        )
        assert events == 1
    finally:
        db.close()


def test_service_execute_requires_approve(stripe_backend):
    _founder, application = _persist_application()
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        instruction = service.prepare(
            backend=stripe_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=50_000,
            target_account_ref="acct_test",
            preparation_method="stripe",
            prepared_by="partner:alice",
        )
        db.commit()
        with pytest.raises(InstructionStateError):
            service.execute(
                backend=stripe_backend,
                instruction=instruction,
                treasurer_id="treasurer:bob",
            )
    finally:
        db.close()


def test_service_full_happy_path(stripe_backend):
    _founder, application = _persist_application()
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        instruction = service.prepare(
            backend=stripe_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=10_000,
            target_account_ref="acct_test",
            preparation_method="stripe",
            prepared_by="partner:alice",
        )
        service.approve(
            instruction=instruction,
            treasurer_id="treasurer:bob",
            note="ok",
        )
        sent = service.execute(
            backend=stripe_backend,
            instruction=instruction,
            treasurer_id="treasurer:bob",
        )
        db.commit()
        assert sent.status == "sent"
        funded_events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investment_funded")
            .count()
        )
        assert funded_events == 1
    finally:
        db.close()


def test_service_dual_approval_required_for_large_amount(bank_backend):
    _founder, application = _persist_application()
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        instruction = service.prepare(
            backend=bank_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=DUAL_APPROVAL_THRESHOLD_USD,
            target_account_ref="cp_mercury_big",
            preparation_method="bank_transfer",
            prepared_by="partner:alice",
        )
        service.approve(
            instruction=instruction,
            treasurer_id="treasurer:bob",
        )
        # Single approval is insufficient at the dual-approval threshold.
        with pytest.raises(InstructionStateError) as exc:
            service.execute(
                backend=bank_backend,
                instruction=instruction,
                treasurer_id="treasurer:bob",
            )
        assert "dual" in str(exc.value)

        # A second distinct treasurer satisfies dual approval.
        service.approve(
            instruction=instruction,
            treasurer_id="treasurer:carol",
        )
        sent = service.execute(
            backend=bank_backend,
            instruction=instruction,
            treasurer_id="treasurer:carol",
        )
        db.commit()
        assert sent.status == "sent"
    finally:
        db.close()


def test_service_repeat_approve_by_same_treasurer_does_not_double_count(stripe_backend):
    _founder, application = _persist_application()
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        instruction = service.prepare(
            backend=stripe_backend,
            application_id=application.id,
            founder_id=application.founder_id,
            amount_usd=DUAL_APPROVAL_THRESHOLD_USD,
            target_account_ref="acct_test",
            preparation_method="stripe",
            prepared_by="partner:alice",
        )
        service.approve(instruction=instruction, treasurer_id="treasurer:bob")
        service.approve(instruction=instruction, treasurer_id="treasurer:bob")
        # Same treasurer twice still only counts as one approval.
        with pytest.raises(InstructionStateError):
            service.execute(
                backend=stripe_backend,
                instruction=instruction,
                treasurer_id="treasurer:bob",
            )
    finally:
        db.close()


def test_service_rejects_invalid_method():
    _founder, application = _persist_application()
    db = SessionLocal()
    try:
        service = CapitalDeployment(db)
        with pytest.raises(CapitalDeploymentError):
            service.prepare(
                backend=None,  # never reached
                application_id=application.id,
                founder_id=application.founder_id,
                amount_usd=10_000,
                target_account_ref="x",
                preparation_method="paypal",
                prepared_by="partner:alice",
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stripe webhook signature
# ---------------------------------------------------------------------------


def _stripe_sign(secret: str, body: bytes, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode("utf-8") + body
    digest = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={digest}"


def test_stripe_signature_accepts_valid_header():
    body = b'{"id":"evt_1"}'
    header = _stripe_sign(STRIPE_WEBHOOK_SECRET, body)
    assert verify_stripe_webhook_signature(STRIPE_WEBHOOK_SECRET, body, header) is True


def test_stripe_signature_rejects_skewed_timestamp():
    body = b'{"id":"evt_1"}'
    stale = int(time.time()) - 600
    header = _stripe_sign(STRIPE_WEBHOOK_SECRET, body, ts=stale)
    assert verify_stripe_webhook_signature(STRIPE_WEBHOOK_SECRET, body, header) is False


def test_stripe_signature_rejects_bad_digest():
    body = b'{"id":"evt_1"}'
    ts = int(time.time())
    header = f"t={ts},v1=" + ("0" * 64)
    assert verify_stripe_webhook_signature(STRIPE_WEBHOOK_SECRET, body, header) is False


# ---------------------------------------------------------------------------
# Router tests via TestClient
# ---------------------------------------------------------------------------


def _make_app(stripe_backend, bank_backend, principal: dict | None = None):
    app = FastAPI()
    app.include_router(capital_router, prefix="/api/v1")

    # Inject a fixed principal via a tiny middleware so `_gate` finds
    # one without needing a real API key in the DB.
    from starlette.middleware.base import BaseHTTPMiddleware

    class _PrincipalStamper(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if principal is not None:
                request.state.principal = dict(principal)
            return await call_next(request)

    app.add_middleware(_PrincipalStamper)

    def _factory(method: str):
        if method == "stripe":
            return stripe_backend
        if method == "bank_transfer":
            return bank_backend
        raise ValueError(method)

    set_backend_factory_for_tests(_factory)
    return app


@pytest.fixture
def partner_client(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    app = _make_app(
        stripe_backend,
        bank_backend,
        principal={
            "auth_type": "test",
            "role": "partner",
            "fingerprint": "alice",
            "key_id": "key_partner",
        },
    )
    yield TestClient(app), application
    reset_backend_factory_for_tests()


@pytest.fixture
def treasurer_client(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    app = _make_app(
        stripe_backend,
        bank_backend,
        principal={
            "auth_type": "test",
            "role": "treasurer",
            "fingerprint": "bob",
            "key_id": "key_treasurer",
        },
    )
    yield TestClient(app), application
    reset_backend_factory_for_tests()


@pytest.fixture
def anonymous_client(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    app = _make_app(stripe_backend, bank_backend, principal=None)
    yield TestClient(app), application
    reset_backend_factory_for_tests()


def test_router_prepare_happy_path(partner_client):
    client, application = partner_client
    res = client.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 10_000,
            "preparation_method": "bank_transfer",
            "target_account_ref": "cp_mercury_xyz",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()["data"]
    assert data["status"] == "prepared"
    assert data["amount_usd"] == 10_000
    assert data["preparation_method"] == "bank_transfer"


def test_router_prepare_validation_error(partner_client):
    client, application = partner_client
    res = client.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 0,
            "preparation_method": "bank_transfer",
            "target_account_ref": "cp_x",
        },
    )
    assert res.status_code == 400


def test_router_prepare_unauthorized_without_principal(anonymous_client):
    client, application = anonymous_client
    res = client.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 10_000,
            "preparation_method": "bank_transfer",
            "target_account_ref": "cp_mercury_xyz",
        },
    )
    assert res.status_code == 401


def test_router_approve_without_prepare_returns_404(treasurer_client):
    client, _application = treasurer_client
    res = client.post(
        "/api/v1/capital/instructions/ins_does_not_exist:approve",
        json={},
    )
    assert res.status_code == 404


def test_router_approve_then_execute(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    # Prepare as partner.
    partner_app = _make_app(
        stripe_backend,
        bank_backend,
        principal={"auth_type": "test", "role": "partner", "fingerprint": "alice", "key_id": "k1"},
    )
    partner = TestClient(partner_app)
    res = partner.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 10_000,
            "preparation_method": "stripe",
            "target_account_ref": "acct_x",
        },
    )
    assert res.status_code == 201
    instruction_id = res.json()["data"]["id"]

    # Approve + execute as treasurer.
    treasurer_app = _make_app(
        stripe_backend,
        bank_backend,
        principal={"auth_type": "test", "role": "treasurer", "fingerprint": "bob", "key_id": "k2"},
    )
    treasurer = TestClient(treasurer_app)
    approve = treasurer.post(
        f"/api/v1/capital/instructions/{instruction_id}:approve",
        json={"note": "looks good"},
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["data"]["instruction"]["status"] == "approved"

    executed = treasurer.post(
        f"/api/v1/capital/instructions/{instruction_id}:execute",
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["data"]["status"] == "sent"
    reset_backend_factory_for_tests()


def test_router_execute_without_approve_returns_403(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    partner_app = _make_app(
        stripe_backend,
        bank_backend,
        principal={"auth_type": "test", "role": "partner", "fingerprint": "alice", "key_id": "k1"},
    )
    partner = TestClient(partner_app)
    res = partner.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 10_000,
            "preparation_method": "stripe",
            "target_account_ref": "acct_x",
        },
    )
    instruction_id = res.json()["data"]["id"]

    treasurer_app = _make_app(
        stripe_backend,
        bank_backend,
        principal={"auth_type": "test", "role": "treasurer", "fingerprint": "bob", "key_id": "k2"},
    )
    treasurer = TestClient(treasurer_app)
    executed = treasurer.post(
        f"/api/v1/capital/instructions/{instruction_id}:execute",
    )
    assert executed.status_code == 403
    assert executed.json()["error"]["code"] == "FORBIDDEN"
    reset_backend_factory_for_tests()


def test_router_partner_cannot_execute(partner_client):
    client, application = partner_client
    res = client.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 10_000,
            "preparation_method": "stripe",
            "target_account_ref": "acct_x",
        },
    )
    instruction_id = res.json()["data"]["id"]
    executed = client.post(
        f"/api/v1/capital/instructions/{instruction_id}:execute",
    )
    # Partner role lacks execute scope.
    assert executed.status_code == 403


def test_router_prepare_idempotent_via_header(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    app = _make_app(
        stripe_backend,
        bank_backend,
        principal={"auth_type": "test", "role": "partner", "fingerprint": "alice", "key_id": "k1"},
    )
    client = TestClient(app)
    body = {
        "application_id": application.id,
        "founder_id": application.founder_id,
        "amount_usd": 10_000,
        "preparation_method": "bank_transfer",
        "target_account_ref": "cp_mercury_xyz",
    }
    headers = {"Idempotency-Key": "req-deadbeef"}
    first = client.post("/api/v1/capital/instructions:prepare", json=body, headers=headers)
    second = client.post("/api/v1/capital/instructions:prepare", json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    reset_backend_factory_for_tests()


def test_router_stripe_webhook_rejects_invalid_signature(stripe_backend, bank_backend):
    _founder, _application = _persist_application()
    app = _make_app(stripe_backend, bank_backend, principal=None)
    client = TestClient(app)
    res = client.post(
        "/api/v1/webhooks/stripe",
        content=b'{"type":"transfer.paid","data":{"object":{"id":"tr_x"}}}',
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert res.status_code == 401
    reset_backend_factory_for_tests()


def test_router_stripe_webhook_marks_instruction_sent(stripe_backend, bank_backend):
    _founder, application = _persist_application()
    # Prepare + approve + execute first.
    app = _make_app(
        stripe_backend,
        bank_backend,
        principal={"auth_type": "test", "role": "treasurer", "fingerprint": "bob", "key_id": "k1"},
    )
    client = TestClient(app)
    prepare = client.post(
        "/api/v1/capital/instructions:prepare",
        json={
            "application_id": application.id,
            "founder_id": application.founder_id,
            "amount_usd": 1_000,
            "preparation_method": "stripe",
            "target_account_ref": "acct_x",
        },
    )
    instruction_id = prepare.json()["data"]["id"]
    client.post(f"/api/v1/capital/instructions/{instruction_id}:approve", json={})
    executed = client.post(f"/api/v1/capital/instructions/{instruction_id}:execute")
    confirmation = executed.json()["data"]["provider_intent_ref"]

    body = json.dumps(
        {
            "type": "transfer.paid",
            "data": {"object": {"id": confirmation}},
        }
    ).encode("utf-8")
    sig = _stripe_sign(STRIPE_WEBHOOK_SECRET, body)
    res = client.post(
        "/api/v1/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "content-type": "application/json"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["instruction_status"] == "sent"
    reset_backend_factory_for_tests()


# ---------------------------------------------------------------------------
# Idempotency-key helper
# ---------------------------------------------------------------------------


def test_compute_idempotency_key_is_stable():
    a = compute_idempotency_key("app1", "stripe", salt="x")
    b = compute_idempotency_key("app1", "stripe", salt="x")
    c = compute_idempotency_key("app1", "stripe", salt="y")
    assert a == b
    assert a != c
