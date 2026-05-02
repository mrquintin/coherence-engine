"""Accredited-investor verification adapter + router tests (prompt 26).

Covers:

* Unit tests on the service / backend layer (HMAC verification, idempotency,
  expiry, replay-as-noop, signature mismatch never mutates state).
* Integration tests on the router via FastAPI ``TestClient``: initiate
  flow returns a deterministic redirect URL, valid signed webhook flips
  the row to ``verified`` and emits the outbox event, invalid signature
  returns 401 and leaves state untouched, replay returns 200 without
  re-mutating.

The tests deliberately do NOT make any real network calls to Persona or
Onfido — webhook signatures are forged locally with the same HMAC-SHA-256
construction the real providers use, and the backends are exercised via
the in-tree synthetic ``initiate`` paths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
from coherence_engine.server.fund.routers.investor_verification import (
    current_investor,
    reset_backend_factory_for_tests,
    router as investor_router,
    set_backend_factory_for_tests,
)
from coherence_engine.server.fund.services import accredited_verification as svc
from coherence_engine.server.fund.services.accredited_backends import (
    InitiationResponse,
    ManualBackend,
    OnfidoBackend,
    PersonaBackend,
    _verify_hmac_sha256,
)


WEBHOOK_SECRET = "persona-test-secret"
ONFIDO_SECRET = "onfido-test-secret"


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


@pytest.fixture
def persona_backend():
    return PersonaBackend(
        api_key="persona-test-api-key",
        webhook_secret=WEBHOOK_SECRET,
        template_id="tmpl_test",
    )


@pytest.fixture
def onfido_backend():
    return OnfidoBackend(
        api_token="onfido-test-token",
        webhook_token=ONFIDO_SECRET,
    )


@pytest.fixture
def manual_backend():
    return ManualBackend(upload_secret="manual-test-secret")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes, ts: int | None = None) -> tuple[str, str]:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return digest, str(ts)


def _persist_investor(sub: str = "inv-sub-1") -> models.Investor:
    db = SessionLocal()
    try:
        inv = models.Investor(
            id=f"inv_{sub}",
            founder_user_id=sub,
            legal_name="Investor One",
            residence_country="US",
            investor_type="individual",
            status="unverified",
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return inv
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HMAC verification unit tests
# ---------------------------------------------------------------------------


def test_hmac_constant_time_compare_accepts_valid_signature():
    body = b'{"a":1}'
    sig, ts = _sign(WEBHOOK_SECRET, body)
    assert _verify_hmac_sha256(
        WEBHOOK_SECRET,
        body,
        signature_header=sig,
        timestamp_header=ts,
    ) is True


def test_hmac_rejects_bad_signature():
    body = b'{"a":1}'
    _, ts = _sign(WEBHOOK_SECRET, body)
    assert _verify_hmac_sha256(
        WEBHOOK_SECRET,
        body,
        signature_header="deadbeef" * 8,
        timestamp_header=ts,
    ) is False


def test_hmac_rejects_skewed_timestamp():
    body = b'{"a":1}'
    stale = int(time.time()) - 600
    sig, ts = _sign(WEBHOOK_SECRET, body, ts=stale)
    assert _verify_hmac_sha256(
        WEBHOOK_SECRET,
        body,
        signature_header=sig,
        timestamp_header=ts,
    ) is False


def test_hmac_rejects_empty_secret():
    body = b'{}'
    sig, ts = _sign(WEBHOOK_SECRET, body)
    assert _verify_hmac_sha256(
        "",
        body,
        signature_header=sig,
        timestamp_header=ts,
    ) is False


def test_hmac_rejects_missing_headers():
    assert _verify_hmac_sha256(
        WEBHOOK_SECRET,
        b'{}',
        signature_header="",
        timestamp_header="",
    ) is False


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


def test_initiate_persists_pending_record(persona_backend):
    investor = _persist_investor()
    db = SessionLocal()
    try:
        record = svc.initiate_verification(
            db, investor=investor, backend=persona_backend
        )
        db.commit()
        assert record.status == "pending"
        assert record.provider == "persona"
        assert record.provider_reference.startswith("per_inq_")
        assert record.idempotency_key
    finally:
        db.close()


def test_initiate_is_idempotent_on_provider_reference(monkeypatch, persona_backend):
    investor = _persist_investor()
    fixed_ref = "per_inq_fixed-1"

    def _stub(self, inv, *, redirect_url=None):
        return InitiationResponse(
            redirect_url="https://withpersona.com/verify",
            provider_reference=fixed_ref,
        )

    monkeypatch.setattr(PersonaBackend, "initiate", _stub)

    db = SessionLocal()
    try:
        a = svc.initiate_verification(db, investor=investor, backend=persona_backend)
        db.commit()
        b = svc.initiate_verification(db, investor=investor, backend=persona_backend)
        db.commit()
        assert a.id == b.id
        assert (
            db.query(models.VerificationRecord)
            .filter(models.VerificationRecord.investor_id == investor.id)
            .count()
            == 1
        )
    finally:
        db.close()


def test_apply_webhook_with_invalid_signature_does_not_mutate(persona_backend):
    investor = _persist_investor()
    db = SessionLocal()
    try:
        record = svc.initiate_verification(
            db, investor=investor, backend=persona_backend
        )
        db.commit()
        ref = record.provider_reference
        body = json.dumps({
            "provider_reference": ref,
            "status": "verified",
            "method": "income",
        }).encode("utf-8")
        with pytest.raises(svc.VerificationError) as excinfo:
            svc.apply_webhook(
                db,
                backend=persona_backend,
                raw_payload=body,
                headers={"persona-signature": "v1=baadf00d", "webhook-timestamp": str(int(time.time()))},
            )
        assert "webhook_signature_invalid" in str(excinfo.value)
        # State must be untouched.
        db.refresh(record)
        assert record.status == "pending"
        assert record.method == "self_certified"
        events = db.query(models.EventOutbox).count()
        assert events == 0
    finally:
        db.close()


def test_apply_webhook_valid_signature_flips_status(persona_backend):
    investor = _persist_investor()
    db = SessionLocal()
    try:
        record = svc.initiate_verification(
            db, investor=investor, backend=persona_backend
        )
        db.commit()
        ref = record.provider_reference
        body = json.dumps({
            "provider_reference": ref,
            "status": "verified",
            "method": "income",
            "evidence_uri": "s3://bucket/k1",
            "evidence_hash": "a" * 64,
        }).encode("utf-8")
        sig, ts = _sign(WEBHOOK_SECRET, body)
        result = svc.apply_webhook(
            db,
            backend=persona_backend,
            raw_payload=body,
            headers={
                "persona-signature": f"t={ts},v1={sig}",
                "webhook-timestamp": ts,
            },
        )
        db.commit()
        assert result is not None
        assert result.id == record.id
        assert result.status == "verified"
        assert result.method == "income"
        assert result.evidence_uri == "s3://bucket/k1"
        assert result.expires_at is not None
        # Investor row also flipped.
        inv = db.query(models.Investor).filter(models.Investor.id == investor.id).one()
        assert inv.status == "verified"
        # Outbox event was enqueued.
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investor_verification_updated")
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["investor_id"] == investor.id
        assert payload["status"] == "verified"
        assert payload["method"] == "income"
    finally:
        db.close()


def test_replay_with_same_state_is_noop(persona_backend):
    investor = _persist_investor()
    db = SessionLocal()
    try:
        record = svc.initiate_verification(
            db, investor=investor, backend=persona_backend
        )
        db.commit()
        ref = record.provider_reference
        body = json.dumps({
            "provider_reference": ref,
            "status": "verified",
            "method": "income",
        }).encode("utf-8")
        sig, ts = _sign(WEBHOOK_SECRET, body)
        headers = {
            "persona-signature": f"t={ts},v1={sig}",
            "webhook-timestamp": ts,
        }
        svc.apply_webhook(
            db, backend=persona_backend, raw_payload=body, headers=headers
        )
        db.commit()
        first_event_count = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investor_verification_updated")
            .count()
        )
        # Replay (same body, same signature) should not double-emit.
        svc.apply_webhook(
            db, backend=persona_backend, raw_payload=body, headers=headers
        )
        db.commit()
        replay_event_count = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investor_verification_updated")
            .count()
        )
        assert replay_event_count == first_event_count == 1
    finally:
        db.close()


def test_expiry_evaluated_lazily():
    investor = _persist_investor()
    from datetime import datetime, timedelta, timezone

    db = SessionLocal()
    try:
        record = models.VerificationRecord(
            id="vrec_expired",
            investor_id=investor.id,
            provider="persona",
            method="income",
            status="verified",
            evidence_uri="s3://k",
            evidence_hash="x" * 64,
            provider_reference="ref-exp",
            idempotency_key="idem-exp",
            error_code="",
            created_at=datetime.now(tz=timezone.utc) - timedelta(days=200),
            completed_at=datetime.now(tz=timezone.utc) - timedelta(days=100),
            expires_at=datetime.now(tz=timezone.utc) - timedelta(days=10),
        )
        db.add(record)
        db.commit()
        effective = svc.evaluate_effective_status(record)
        assert effective == "expired"
        # Stored row is unchanged.
        db.refresh(record)
        assert record.status == "verified"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Manual backend always rejects "webhook" requests
# ---------------------------------------------------------------------------


def test_manual_backend_webhook_signature_always_false(manual_backend):
    assert manual_backend.webhook_signature_ok(b"{}", {"x-anything": "yes"}) is False


def test_manual_backend_initiate_returns_upload_token(manual_backend):
    investor = _persist_investor()
    response = manual_backend.initiate(investor)
    assert response.upload_token
    assert response.redirect_url == ""
    assert response.provider_reference.startswith("man_")


# ---------------------------------------------------------------------------
# Router tests via TestClient
# ---------------------------------------------------------------------------


def _make_app(investor: models.Investor, persona_backend):
    """Build a router-only FastAPI app with current_investor stubbed.

    We replace ``current_investor`` directly via dependency-overrides so
    the test does not need a real Supabase JWT — that contract is
    covered separately in ``tests/test_route_auth_gating.py``.
    """
    app = FastAPI()
    app.include_router(investor_router, prefix="/api/v1")
    app.include_router(investor_router)

    def _override():
        # Re-fetch from DB to avoid detached-instance issues.
        db = SessionLocal()
        try:
            return (
                db.query(models.Investor)
                .filter(models.Investor.id == investor.id)
                .one()
            )
        finally:
            db.close()

    app.dependency_overrides[current_investor] = _override

    set_backend_factory_for_tests(lambda provider: persona_backend if provider == "persona" else _raise(provider))
    return app


def _raise(provider):
    raise ValueError(f"backend factory not configured for {provider}")


@pytest.fixture
def app_client(persona_backend):
    investor = _persist_investor()
    app = _make_app(investor, persona_backend)
    yield TestClient(app), investor
    reset_backend_factory_for_tests()


def test_router_initiate_returns_redirect_url(app_client):
    client, investor = app_client
    res = client.post(
        f"/api/v1/investors/{investor.id}/verification:initiate",
        json={"provider": "persona"},
    )
    assert res.status_code == 201, res.text
    body = res.json()["data"]
    assert body["provider"] == "persona"
    assert body["redirect_url"].startswith("https://withpersona.com/verify")
    assert body["status"] == "pending"
    assert body["record_id"]
    assert body["provider_reference"].startswith("per_inq_")


def test_router_get_verification_returns_latest(app_client):
    client, investor = app_client
    client.post(
        f"/api/v1/investors/{investor.id}/verification:initiate",
        json={"provider": "persona"},
    )
    res = client.get(f"/api/v1/investors/{investor.id}/verification")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "pending"
    assert data["record"]["provider"] == "persona"


def test_router_webhook_invalid_signature_returns_401(app_client, persona_backend):
    client, investor = app_client
    init = client.post(
        f"/api/v1/investors/{investor.id}/verification:initiate",
        json={"provider": "persona"},
    )
    ref = init.json()["data"]["provider_reference"]
    body = json.dumps({
        "provider_reference": ref,
        "status": "verified",
        "method": "income",
    })
    res = client.post(
        "/api/v1/webhooks/persona",
        data=body,
        headers={
            "content-type": "application/json",
            "persona-signature": "v1=baadf00d",
            "webhook-timestamp": str(int(time.time())),
        },
    )
    assert res.status_code == 401
    # Confirm no row was mutated.
    db = SessionLocal()
    try:
        rec = (
            db.query(models.VerificationRecord)
            .filter(models.VerificationRecord.investor_id == investor.id)
            .one()
        )
        assert rec.status == "pending"
    finally:
        db.close()


def test_router_webhook_valid_signature_flips_and_replay_is_noop(app_client):
    client, investor = app_client
    init = client.post(
        f"/api/v1/investors/{investor.id}/verification:initiate",
        json={"provider": "persona"},
    )
    ref = init.json()["data"]["provider_reference"]
    body = json.dumps({
        "provider_reference": ref,
        "status": "verified",
        "method": "income",
    }).encode("utf-8")
    sig, ts = _sign(WEBHOOK_SECRET, body)
    headers = {
        "content-type": "application/json",
        "persona-signature": f"t={ts},v1={sig}",
        "webhook-timestamp": ts,
    }
    res = client.post("/api/v1/webhooks/persona", content=body, headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "verified"

    # Replay: same body, same signature, same headers.
    res2 = client.post("/api/v1/webhooks/persona", content=body, headers=headers)
    assert res2.status_code == 200, res2.text

    # Outbox should hold exactly one event for this record (replay is no-op).
    db = SessionLocal()
    try:
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "investor_verification_updated")
            .all()
        )
        assert len(events) == 1
    finally:
        db.close()


def test_router_unknown_provider_400(app_client):
    client, investor = app_client
    res = client.post(
        f"/api/v1/investors/{investor.id}/verification:initiate",
        json={"provider": "totally-fake"},
    )
    assert res.status_code == 400


def test_router_get_when_no_record(app_client):
    client, investor = app_client
    res = client.get(f"/api/v1/investors/{investor.id}/verification")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "absent"
    assert data["record"] is None
