"""Founder KYC/AML adapter + decision-policy gate tests (prompt 53).

Covers:

* Service / backend layer: HMAC verification, idempotency, expiry,
  replay-as-noop, signature mismatch never mutates state, screening-
  category serialization.
* Decision-policy ``kyc_clear`` gate: a ``pass`` verdict downgrades
  to ``manual_review`` with reason ``KYC_REQUIRED`` when KYC is
  missing/expired, and the same inputs flow to ``pass`` once KYC is
  cleared.
* Refresh-due cadence: :func:`scan_refresh_due` emits one
  ``founder_kyc.refresh_due`` event per row that is within
  :data:`KYC_REFRESH_NOTICE_DAYS` of expiry, and is idempotent across
  re-runs of the daily job.
* Webhook signature verification (router): valid signature flips the
  row, invalid signature returns 401 and leaves state untouched.

The tests deliberately do NOT make any real network calls -- webhook
signatures are forged locally with the same HMAC-SHA-256 construction
the real providers use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

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
from coherence_engine.server.fund.routers.founder_kyc import (
    current_founder_kyc,
    reset_kyc_backend_factory_for_tests,
    router as founder_kyc_router,
    set_kyc_backend_factory_for_tests,
)
from coherence_engine.server.fund.services import founder_kyc as svc
from coherence_engine.server.fund.services.decision_policy import (
    DecisionPolicyService,
)
from coherence_engine.server.fund.services.founder_kyc_backends import (
    OnfidoKYCBackend,
    PersonaKYCBackend,
    _verify_hmac_sha256,
)


PERSONA_KYC_SECRET = "persona-kyc-test-secret"
ONFIDO_KYC_SECRET = "onfido-kyc-test-secret"


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
    return PersonaKYCBackend(
        api_key="persona-kyc-api-key",
        webhook_secret=PERSONA_KYC_SECRET,
        template_id="tmpl_kyc",
    )


@pytest.fixture
def onfido_backend():
    return OnfidoKYCBackend(
        api_token="onfido-kyc-token",
        webhook_token=ONFIDO_KYC_SECRET,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes, ts: int | None = None) -> tuple[str, str]:
    if ts is None:
        ts = int(time.time())
    signed = f"{ts}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return digest, str(ts)


def _persist_founder(suffix: str = "1") -> models.Founder:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id=f"fnd_kyc_{suffix}",
            full_name=f"Founder {suffix}",
            email=f"founder{suffix}@example.com",
            company_name=f"Company {suffix}",
            country="US",
            founder_user_id=f"sub-kyc-{suffix}",
        )
        db.add(founder)
        db.commit()
        db.refresh(founder)
        return founder
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def test_hmac_accepts_valid_signature():
    body = b'{"a":1}'
    sig, ts = _sign(PERSONA_KYC_SECRET, body)
    assert (
        _verify_hmac_sha256(
            PERSONA_KYC_SECRET,
            body,
            signature_header=sig,
            timestamp_header=ts,
        )
        is True
    )


def test_hmac_rejects_bad_signature():
    body = b'{"a":1}'
    _, ts = _sign(PERSONA_KYC_SECRET, body)
    assert (
        _verify_hmac_sha256(
            PERSONA_KYC_SECRET,
            body,
            signature_header="deadbeef" * 8,
            timestamp_header=ts,
        )
        is False
    )


def test_hmac_rejects_skewed_timestamp():
    body = b'{"a":1}'
    stale = int(time.time()) - 600
    sig, ts = _sign(PERSONA_KYC_SECRET, body, ts=stale)
    assert (
        _verify_hmac_sha256(
            PERSONA_KYC_SECRET,
            body,
            signature_header=sig,
            timestamp_header=ts,
        )
        is False
    )


# ---------------------------------------------------------------------------
# Service-layer
# ---------------------------------------------------------------------------


def test_initiate_persists_pending_record(persona_backend):
    founder = _persist_founder()
    db = SessionLocal()
    try:
        record = svc.initiate_kyc(
            db,
            founder=founder,
            backend=persona_backend,
            screening_categories=["sanctions", "pep", "id"],
        )
        db.commit()
        assert record.status == "pending"
        assert record.provider == "persona"
        assert record.provider_reference.startswith("per_kyc_")
        assert record.idempotency_key
        # Categories normalize: sorted, deduplicated, lowercased.
        assert record.screening_categories == "id,pep,sanctions"
    finally:
        db.close()


def test_initiate_idempotent_on_provider_reference(monkeypatch, persona_backend):
    founder = _persist_founder()
    fixed_ref = "per_kyc_fixed-1"

    from coherence_engine.server.fund.services.founder_kyc_backends import (
        KYCInitiationResponse,
    )

    def _stub(self, fnd, *, redirect_url=None):
        return KYCInitiationResponse(
            redirect_url="https://withpersona.com/kyc",
            provider_reference=fixed_ref,
        )

    monkeypatch.setattr(PersonaKYCBackend, "initiate", _stub)

    db = SessionLocal()
    try:
        a = svc.initiate_kyc(db, founder=founder, backend=persona_backend)
        db.commit()
        b = svc.initiate_kyc(db, founder=founder, backend=persona_backend)
        db.commit()
        assert a.id == b.id
        count = (
            db.query(models.KYCResult)
            .filter(models.KYCResult.founder_id == founder.id)
            .count()
        )
        assert count == 1
    finally:
        db.close()


def test_apply_webhook_invalid_signature_does_not_mutate(persona_backend):
    founder = _persist_founder()
    db = SessionLocal()
    try:
        record = svc.initiate_kyc(
            db, founder=founder, backend=persona_backend
        )
        db.commit()
        ref = record.provider_reference
        body = json.dumps(
            {
                "provider_reference": ref,
                "status": "passed",
            }
        ).encode("utf-8")
        with pytest.raises(svc.KYCError) as excinfo:
            svc.apply_webhook(
                db,
                backend=persona_backend,
                raw_payload=body,
                headers={
                    "persona-signature": "v1=baadf00d",
                    "webhook-timestamp": str(int(time.time())),
                },
            )
        assert "webhook_signature_invalid" in str(excinfo.value)
        # State must be untouched.
        db.refresh(record)
        assert record.status == "pending"
        events = db.query(models.EventOutbox).count()
        assert events == 0
    finally:
        db.close()


def test_apply_webhook_valid_signature_flips_status(persona_backend):
    founder = _persist_founder()
    db = SessionLocal()
    try:
        record = svc.initiate_kyc(
            db, founder=founder, backend=persona_backend
        )
        db.commit()
        ref = record.provider_reference
        body = json.dumps(
            {
                "provider_reference": ref,
                "status": "passed",
                "screening_categories": ["sanctions", "pep", "id", "aml"],
                "evidence_uri": "s3://kyc/1",
                "evidence_hash": "a" * 64,
            }
        ).encode("utf-8")
        sig, ts = _sign(PERSONA_KYC_SECRET, body)
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
        assert result.status == "passed"
        assert result.evidence_uri == "s3://kyc/1"
        assert result.expires_at is not None
        # Annual TTL.
        assert result.expires_at - result.completed_at > timedelta(days=300)
        # Refresh notice scheduled 30d before expiry.
        assert result.refresh_required_at is not None
        delta = result.expires_at - result.refresh_required_at
        assert delta == timedelta(days=svc.KYC_REFRESH_NOTICE_DAYS)
        # Outbox event emitted.
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "founder_kyc.updated")
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["founder_id"] == founder.id
        assert payload["status"] == "passed"
        assert "sanctions" in payload["screening_categories"]
    finally:
        db.close()


def test_replay_with_same_state_is_noop(persona_backend):
    founder = _persist_founder()
    db = SessionLocal()
    try:
        record = svc.initiate_kyc(
            db, founder=founder, backend=persona_backend
        )
        db.commit()
        ref = record.provider_reference
        body = json.dumps(
            {
                "provider_reference": ref,
                "status": "passed",
            }
        ).encode("utf-8")
        sig, ts = _sign(PERSONA_KYC_SECRET, body)
        headers = {
            "persona-signature": f"t={ts},v1={sig}",
            "webhook-timestamp": ts,
        }
        svc.apply_webhook(
            db, backend=persona_backend, raw_payload=body, headers=headers
        )
        db.commit()
        first_count = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "founder_kyc.updated")
            .count()
        )
        svc.apply_webhook(
            db, backend=persona_backend, raw_payload=body, headers=headers
        )
        db.commit()
        replay_count = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == "founder_kyc.updated")
            .count()
        )
        assert first_count == replay_count == 1
    finally:
        db.close()


def test_expiry_evaluated_lazily():
    founder = _persist_founder()
    db = SessionLocal()
    try:
        record = models.KYCResult(
            id="kyc_expired",
            founder_id=founder.id,
            provider="persona",
            status="passed",
            screening_categories="sanctions,pep,id,aml",
            evidence_uri="s3://k",
            evidence_hash="x" * 64,
            provider_reference="ref-exp",
            idempotency_key="idem-exp",
            error_code="",
            failure_reason="",
            created_at=datetime.now(tz=timezone.utc) - timedelta(days=400),
            completed_at=datetime.now(tz=timezone.utc) - timedelta(days=370),
            expires_at=datetime.now(tz=timezone.utc) - timedelta(days=5),
            refresh_required_at=datetime.now(tz=timezone.utc) - timedelta(days=35),
        )
        db.add(record)
        db.commit()
        assert svc.evaluate_effective_status(record) == "expired"
        assert svc.is_kyc_clear(record) is False
        # Stored row unchanged.
        db.refresh(record)
        assert record.status == "passed"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Decision-policy ``kyc_clear`` gate
# ---------------------------------------------------------------------------


def _baseline_application(**overrides):
    base = {
        "domain_primary": "market_economics",
        "requested_check_usd": 60000,
        "compliance_status": "clear",
    }
    base.update(overrides)
    return base


def _baseline_score():
    # CI lower=0.30 comfortably exceeds cs_required at 60k requested
    # ($60k / S_min=$50k -> log2 ~ 0.26 -> cs_required ~ 0.18 + 0.25*0.26 = 0.245).
    return {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.05,
        "coherence_superiority_ci95": {"lower": 0.30, "upper": 0.40},
    }


def test_pass_downgrades_to_manual_review_when_kyc_missing():
    """A would-be ``pass`` becomes ``manual_review`` with KYC_REQUIRED."""
    policy = DecisionPolicyService()
    result = policy.evaluate(
        application=_baseline_application(kyc_passed=False),
        score_record=_baseline_score(),
    )
    assert result["decision"] == "manual_review"
    codes = [g["reason_code"] for g in result["failed_gates"]]
    assert "KYC_REQUIRED" in codes
    gates = [g["gate"] for g in result["failed_gates"]]
    assert "kyc_clear" in gates


def test_pass_when_kyc_passes():
    """Same inputs flow to ``pass`` once KYC is cleared."""
    policy = DecisionPolicyService()
    result = policy.evaluate(
        application=_baseline_application(kyc_passed=True),
        score_record=_baseline_score(),
    )
    assert result["decision"] == "pass"
    codes = [g["reason_code"] for g in result["failed_gates"]]
    assert "KYC_REQUIRED" not in codes


def test_kyc_gate_omitted_field_is_backward_compatible():
    """Callers that don't thread ``kyc_passed`` get the pre-prompt-53 behavior."""
    policy = DecisionPolicyService()
    result = policy.evaluate(
        application=_baseline_application(),  # no kyc_passed key
        score_record=_baseline_score(),
    )
    assert result["decision"] == "pass"


def test_kyc_gate_does_not_override_hard_fail():
    """A hard-fail input stays ``fail`` even when KYC also fails -- the
    operator's manual-review surface is for *otherwise-passing* applications."""
    policy = DecisionPolicyService()
    result = policy.evaluate(
        application=_baseline_application(
            kyc_passed=False, compliance_status="blocked"
        ),
        score_record=_baseline_score(),
    )
    assert result["decision"] == "fail"


# ---------------------------------------------------------------------------
# Refresh-due cadence
# ---------------------------------------------------------------------------


def test_refresh_due_emits_event_within_notice_window():
    founder = _persist_founder("ref")
    db = SessionLocal()
    try:
        now = datetime.now(tz=timezone.utc)
        record = models.KYCResult(
            id="kyc_near_expiry",
            founder_id=founder.id,
            provider="persona",
            status="passed",
            screening_categories="sanctions,pep,id,aml",
            evidence_uri="",
            evidence_hash="",
            provider_reference="ref-near",
            idempotency_key="idem-near",
            error_code="",
            failure_reason="",
            created_at=now - timedelta(days=350),
            completed_at=now - timedelta(days=350),
            expires_at=now + timedelta(days=15),
            refresh_required_at=now - timedelta(days=15),
        )
        db.add(record)
        db.commit()

        emitted = svc.scan_refresh_due(db)
        db.commit()
        assert emitted == 1

        events = (
            db.query(models.EventOutbox)
            .filter(
                models.EventOutbox.event_type == "founder_kyc.refresh_due"
            )
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["founder_id"] == founder.id
        assert payload["days_remaining"] in {14, 15}

        # Idempotent: re-running the daily scan does not double-emit.
        again = svc.scan_refresh_due(db)
        db.commit()
        assert again == 0
        events_after = (
            db.query(models.EventOutbox)
            .filter(
                models.EventOutbox.event_type == "founder_kyc.refresh_due"
            )
            .count()
        )
        assert events_after == 1
    finally:
        db.close()


def test_refresh_due_skips_rows_outside_window():
    founder = _persist_founder("far")
    db = SessionLocal()
    try:
        now = datetime.now(tz=timezone.utc)
        record = models.KYCResult(
            id="kyc_far_expiry",
            founder_id=founder.id,
            provider="persona",
            status="passed",
            screening_categories="sanctions,pep,id,aml",
            evidence_uri="",
            evidence_hash="",
            provider_reference="ref-far",
            idempotency_key="idem-far",
            error_code="",
            failure_reason="",
            created_at=now - timedelta(days=10),
            completed_at=now - timedelta(days=10),
            expires_at=now + timedelta(days=200),
            refresh_required_at=now + timedelta(days=170),
        )
        db.add(record)
        db.commit()
        emitted = svc.scan_refresh_due(db)
        assert emitted == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router tests (FastAPI TestClient)
# ---------------------------------------------------------------------------


def _make_app(founder, persona_backend):
    app = FastAPI()
    app.include_router(founder_kyc_router, prefix="/api/v1")
    app.include_router(founder_kyc_router)

    def _override():
        db = SessionLocal()
        try:
            return (
                db.query(models.Founder)
                .filter(models.Founder.id == founder.id)
                .one()
            )
        finally:
            db.close()

    app.dependency_overrides[current_founder_kyc] = _override
    set_kyc_backend_factory_for_tests(
        lambda provider: persona_backend
        if provider == "persona"
        else _raise(provider)
    )
    return app


def _raise(provider):
    raise ValueError(f"backend factory not configured for {provider}")


@pytest.fixture
def app_client(persona_backend):
    founder = _persist_founder("rt")
    app = _make_app(founder, persona_backend)
    yield TestClient(app), founder
    reset_kyc_backend_factory_for_tests()


def test_router_initiate_returns_redirect_url(app_client):
    client, founder = app_client
    res = client.post(
        f"/api/v1/founders/{founder.id}/kyc:initiate",
        json={"provider": "persona"},
    )
    assert res.status_code == 201, res.text
    body = res.json()["data"]
    assert body["provider"] == "persona"
    assert body["redirect_url"].startswith("https://withpersona.com/kyc")
    assert body["status"] == "pending"
    assert body["result_id"]
    assert body["provider_reference"].startswith("per_kyc_")


def test_router_get_kyc_returns_latest(app_client):
    client, founder = app_client
    client.post(
        f"/api/v1/founders/{founder.id}/kyc:initiate",
        json={"provider": "persona"},
    )
    res = client.get(f"/api/v1/founders/{founder.id}/kyc")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "pending"
    assert data["result"]["provider"] == "persona"


def test_router_webhook_invalid_signature_returns_401(app_client):
    client, founder = app_client
    init = client.post(
        f"/api/v1/founders/{founder.id}/kyc:initiate",
        json={"provider": "persona"},
    )
    ref = init.json()["data"]["provider_reference"]
    body = json.dumps(
        {"provider_reference": ref, "status": "passed"}
    ).encode("utf-8")
    res = client.post(
        "/api/v1/webhooks/founder_kyc/persona",
        content=body,
        headers={
            "persona-signature": "v1=deadbeef",
            "webhook-timestamp": str(int(time.time())),
            "content-type": "application/json",
        },
    )
    assert res.status_code == 401
    # No row mutation.
    db = SessionLocal()
    try:
        latest = svc.latest_result_for_founder(db, founder.id)
        assert latest.status == "pending"
    finally:
        db.close()


def test_router_webhook_valid_signature_flips_to_passed(app_client):
    client, founder = app_client
    init = client.post(
        f"/api/v1/founders/{founder.id}/kyc:initiate",
        json={"provider": "persona"},
    )
    ref = init.json()["data"]["provider_reference"]
    body = json.dumps(
        {
            "provider_reference": ref,
            "status": "passed",
            "screening_categories": ["sanctions", "pep", "id", "aml"],
        }
    ).encode("utf-8")
    sig, ts = _sign(PERSONA_KYC_SECRET, body)
    res = client.post(
        "/api/v1/webhooks/founder_kyc/persona",
        content=body,
        headers={
            "persona-signature": f"t={ts},v1={sig}",
            "webhook-timestamp": ts,
            "content-type": "application/json",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "passed"
    db = SessionLocal()
    try:
        latest = svc.latest_result_for_founder(db, founder.id)
        assert svc.is_kyc_clear(latest) is True
    finally:
        db.close()
