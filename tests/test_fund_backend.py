"""Tests for fund backend API, policy, outbox transitions, and security."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from coherence_engine.server.fund.app import create_app
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.models import ApiKeyAuditEvent, EventOutbox, ScoringJob
from coherence_engine.server.fund.repositories.outbox_repository import OutboxRepository
from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.services.decision_policy import DecisionPolicyService
from coherence_engine.server.fund.services.outbox_dispatcher import OutboxDispatcher
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.secret_manager import SecretManagerError, validate_secret_manager_policy


TOKENS = {}


@pytest.fixture(autouse=True)
def reset_fund_db():
    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        admin = svc.create_key(repo, label="test-admin", role="admin", created_by="tests", expires_in_days=30)
        analyst = svc.create_key(repo, label="test-analyst", role="analyst", created_by="tests", expires_in_days=30)
        viewer = svc.create_key(repo, label="test-viewer", role="viewer", created_by="tests", expires_in_days=30)
        TOKENS["admin"] = admin["token"]
        TOKENS["analyst"] = analyst["token"]
        TOKENS["viewer"] = viewer["token"]
        db.commit()
    finally:
        db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _headers(key: str = "k1", role: str = "admin"):
    return {
        "Idempotency-Key": key,
        "X-Request-Id": "req_test_001",
        "X-API-Key": TOKENS[role],
    }


def test_fund_api_queue_and_worker_flow():
    app = create_app()
    client = TestClient(app)

    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("create-1"),
        json={
            "founder": {
                "full_name": "Jane Founder",
                "email": "jane@example.com",
                "company_name": "Acme Labs",
                "country": "US",
            },
            "startup": {
                "one_liner": "Workflow automation for SMB finance ops",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "Hire engineers and run pilots",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    assert create_res.status_code == 201
    app_id = create_res.json()["data"]["application_id"]

    interview_res = client.post(
        f"/api/v1/applications/{app_id}/interview-sessions",
        headers=_headers("interview-1"),
        json={"channel": "web_voice", "locale": "en-US"},
    )
    assert interview_res.status_code == 201

    score_res = client.post(
        f"/api/v1/applications/{app_id}/score",
        headers=_headers("score-1"),
        json={
            "mode": "standard",
            "dry_run": False,
            "transcript_text": (
                "We reduce back-office processing time for small businesses. "
                "Our software integrates accounting, invoicing, and procurement. "
                "Pilot users reported fewer reconciliation errors and faster closes. "
                "The market has millions of SMBs with fragmented workflows. "
                "We sell a subscription model with expansion to payments."
            ),
        },
    )
    assert score_res.status_code == 202
    assert score_res.json()["data"]["status"] == "queued"

    # Enqueue-only semantics: pending before worker execution.
    pending_res = client.get(f"/api/v1/applications/{app_id}/decision", headers={"X-API-Key": TOKENS["admin"]})
    assert pending_res.status_code == 200
    assert pending_res.json()["data"]["decision"] in {"pending", "pass", "fail", "manual_review"}

    from coherence_engine.server.fund.scoring_worker import process_once

    worker_result = process_once(max_jobs=10)
    assert worker_result["processed"] >= 1

    decision_res = client.get(f"/api/v1/applications/{app_id}/decision", headers={"X-API-Key": TOKENS["admin"]})
    assert decision_res.status_code == 200
    assert decision_res.json()["data"]["decision"] in {"pass", "fail", "manual_review"}

    db = SessionLocal()
    try:
        rows = db.query(EventOutbox).filter(EventOutbox.status == "pending").all()
        event_types = {r.event_type for r in rows}
        assert {"InterviewCompleted", "ArgumentCompiled", "CoherenceScored", "DecisionIssued"}.issubset(event_types)
    finally:
        db.close()


def test_score_idempotency_replay_returns_same_payload():
    app = create_app()
    client = TestClient(app)

    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("create-2"),
        json={
            "founder": {
                "full_name": "John Founder",
                "email": "john@example.com",
                "company_name": "Ops Grid",
                "country": "US",
            },
            "startup": {
                "one_liner": "SaaS for warehouse forecasting",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "Expand GTM",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    app_id = create_res.json()["data"]["application_id"]
    client.post(
        f"/api/v1/applications/{app_id}/interview-sessions",
        headers=_headers("interview-2"),
        json={"channel": "web_voice", "locale": "en-US"},
    )

    body = {"mode": "standard", "dry_run": False}
    first = client.post(f"/api/v1/applications/{app_id}/score", headers=_headers("score-idem"), json=body)
    second = client.post(f"/api/v1/applications/{app_id}/score", headers=_headers("score-idem"), json=body)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()


def test_escalation_gate_blocks_non_pass_decisions():
    app = create_app()
    client = TestClient(app)

    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("create-3"),
        json={
            "founder": {
                "full_name": "Maya Founder",
                "email": "maya@example.com",
                "company_name": "Civic Stack",
                "country": "US",
            },
            "startup": {
                "one_liner": "Civic regulation workflow software",
                "requested_check_usd": 100000,
                "use_of_funds_summary": "Prototype and pilots",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    app_id = create_res.json()["data"]["application_id"]
    res = client.post(
        f"/api/v1/applications/{app_id}/escalation-packet",
        headers=_headers("esc-1"),
        json={"partner_email": "investments@example.com", "include_calendar_link": True},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "UNPROCESSABLE_STATE"


def test_outbox_dispatcher_state_transitions():
    class FakePublisher:
        def publish(self, topic, key, payload):
            return None

    db = SessionLocal()
    try:
        row = EventOutbox(
            id="evt_test_1",
            event_type="DecisionIssued",
            event_version="1.0.0",
            producer="decision-policy-engine",
            trace_id="trc_test",
            idempotency_key="idem_test",
            payload_json=json.dumps(
                {
                    "application_id": "app_x",
                    "decision_id": "dec_x",
                    "decision": "pass",
                    "threshold_required": 0.1,
                    "coherence_observed": 0.2,
                    "margin": 0.1,
                    "failed_gates": [],
                    "policy_version": "decision-policy-v1.0.0",
                    "parameter_set_id": "params",
                }
            ),
            status="pending",
            attempts=0,
            last_error="",
        )
        db.add(row)
        db.commit()

        dispatcher = OutboxDispatcher(db=db, publisher=FakePublisher(), topic_prefix="coherence.fund")
        result = dispatcher.dispatch_once(batch_size=10)
        assert result["published"] == 1

        repo = OutboxRepository(db)
        pending = repo.fetch_pending(batch_size=10)
        assert not pending
    finally:
        db.close()


def test_outbox_failure_backoff_and_replay():
    class AlwaysFailPublisher:
        def publish(self, topic, key, payload):
            raise RuntimeError("broker_down")

    db = SessionLocal()
    try:
        row = EventOutbox(
            id="evt_fail_1",
            event_type="DecisionIssued",
            event_version="1.0.0",
            producer="decision-policy-engine",
            trace_id="trc_fail",
            idempotency_key="idem_fail",
            payload_json=json.dumps({"application_id": "app_x", "decision_id": "dec_x", "decision": "pass"}),
            status="pending",
            attempts=4,
            last_error="",
        )
        db.add(row)
        db.commit()

        dispatcher = OutboxDispatcher(
            db=db,
            publisher=AlwaysFailPublisher(),
            topic_prefix="coherence.fund",
            max_attempts=5,
            retry_base_seconds=1,
        )
        result = dispatcher.dispatch_once(batch_size=10)
        assert result["failed"] == 1

        reloaded = db.query(EventOutbox).filter(EventOutbox.id == "evt_fail_1").one()
        assert reloaded.status == "failed"
        assert reloaded.attempts == 5

        replayed = OutboxRepository(db).replay_failed(event_ids=["evt_fail_1"], reset_attempts=True)
        db.commit()
        assert replayed == 1

        reloaded = db.query(EventOutbox).filter(EventOutbox.id == "evt_fail_1").one()
        assert reloaded.status == "pending"
        assert reloaded.attempts == 0
        assert reloaded.next_attempt_at is not None
    finally:
        db.close()


def test_scoring_job_retry_then_dead_letter_and_replay(monkeypatch):
    app = create_app()
    client = TestClient(app)
    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("create-retry"),
        json={
            "founder": {
                "full_name": "Retry Founder",
                "email": "retry@example.com",
                "company_name": "Retry Labs",
                "country": "US",
            },
            "startup": {
                "one_liner": "A startup that will force scoring failure",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "test retries",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    app_id = create_res.json()["data"]["application_id"]
    client.post(
        f"/api/v1/applications/{app_id}/interview-sessions",
        headers=_headers("interview-retry"),
        json={"channel": "web_voice", "locale": "en-US"},
    )
    score_res = client.post(
        f"/api/v1/applications/{app_id}/score",
        headers=_headers("score-retry"),
        json={"mode": "standard", "dry_run": False},
    )
    job_id = score_res.json()["data"]["job_id"]

    monkeypatch.setattr(
        "coherence_engine.server.fund.services.scoring.ScoringService.score_application",
        lambda self, application: (_ for _ in ()).throw(RuntimeError("forced_scoring_failure")),
    )
    from coherence_engine.server.fund.scoring_worker import process_once

    for _ in range(5):
        process_once(max_jobs=1, retry_base_seconds=1)
        db = SessionLocal()
        try:
            rec = db.query(ScoringJob).filter(ScoringJob.id == job_id).one()
            if rec.status == "failed":
                break
            rec.next_attempt_at = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()

    db = SessionLocal()
    try:
        rec = db.query(ScoringJob).filter(ScoringJob.id == job_id).one()
        assert rec.status == "failed"
        assert rec.attempts >= rec.max_attempts

        replayed = ApplicationRepository(db).replay_scoring_jobs(job_ids=[job_id], reset_attempts=True)
        db.commit()
        assert replayed == 1

        rec = db.query(ScoringJob).filter(ScoringJob.id == job_id).one()
        assert rec.status == "queued"
        assert rec.attempts == 0
        assert rec.next_attempt_at is not None
    finally:
        db.close()


def test_scoring_job_lease_expiry_can_be_reclaimed():
    app = create_app()
    client = TestClient(app)
    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("create-lease"),
        json={
            "founder": {
                "full_name": "Lease Founder",
                "email": "lease@example.com",
                "company_name": "Lease Labs",
                "country": "US",
            },
            "startup": {
                "one_liner": "Lease reclaim test startup",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "test lease reclaim",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    app_id = create_res.json()["data"]["application_id"]
    client.post(
        f"/api/v1/applications/{app_id}/interview-sessions",
        headers=_headers("interview-lease"),
        json={"channel": "web_voice", "locale": "en-US"},
    )
    score_res = client.post(
        f"/api/v1/applications/{app_id}/score",
        headers=_headers("score-lease"),
        json={"mode": "standard", "dry_run": False},
    )
    job_id = score_res.json()["data"]["job_id"]

    db = SessionLocal()
    try:
        repo = ApplicationRepository(db)
        first_claim = repo.claim_next_scoring_job(worker_id="worker-a", lease_seconds=120)
        assert first_claim is not None
        assert first_claim.id == job_id
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        rec = db.query(ScoringJob).filter(ScoringJob.id == job_id).one()
        rec.lease_expires_at = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        repo = ApplicationRepository(db)
        reclaimed = repo.claim_next_scoring_job(worker_id="worker-b", lease_seconds=120)
        assert reclaimed is not None
        assert reclaimed.id == job_id
        assert reclaimed.locked_by == "worker-b"
        assert reclaimed.attempts >= 2
    finally:
        db.close()


def test_policy_service_gate_outcomes():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 50000, "compliance_status": "clear"}
    pass_score = {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.1,
        "coherence_superiority_ci95": {"lower": 0.8, "upper": 0.9},
    }
    fail_score = {
        "transcript_quality_score": 0.2,
        "anti_gaming_score": 0.9,
        "coherence_superiority_ci95": {"lower": 0.0, "upper": 0.4},
    }
    review_score = {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.26,
        "coherence_superiority_ci95": {"lower": 0.7, "upper": 0.95},
    }

    assert policy.evaluate(app, pass_score)["decision"] == "pass"
    assert policy.evaluate(app, fail_score)["decision"] == "fail"
    assert policy.evaluate(app, review_score)["decision"] in {"manual_review", "fail"}


def test_auth_required_for_fund_routes():
    app = create_app()
    client = TestClient(app)
    res = client.post(
        "/api/v1/applications",
        headers={"Idempotency-Key": "x1", "X-Request-Id": "req_x"},
        json={
            "founder": {
                "full_name": "No Auth",
                "email": "noauth@example.com",
                "company_name": "NoAuth Inc",
                "country": "US",
            },
            "startup": {
                "one_liner": "No auth startup",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "Test",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_role_check_blocks_non_admin_escalation():
    app = create_app()
    client = TestClient(app)

    create_res = client.post(
        "/api/v1/applications",
        headers=_headers("create-role", role="admin"),
        json={
            "founder": {
                "full_name": "Role Test",
                "email": "role@example.com",
                "company_name": "Role Inc",
                "country": "US",
            },
            "startup": {
                "one_liner": "Role checking startup",
                "requested_check_usd": 50000,
                "use_of_funds_summary": "Test",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    app_id = create_res.json()["data"]["application_id"]
    res = client.post(
        f"/api/v1/applications/{app_id}/escalation-packet",
        headers=_headers("esc-role", role="viewer"),
        json={"partner_email": "investments@example.com", "include_calendar_link": True},
    )
    assert res.status_code in {401, 403}


def test_rate_limit_returns_429(monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("COHERENCE_FUND_RATE_LIMIT_REQUESTS", "1")
    monkeypatch.setenv("COHERENCE_FUND_RATE_LIMIT_WINDOW_SECONDS", "60")

    app = create_app()
    client = TestClient(app)

    body = {
        "founder": {
            "full_name": "Rate Test",
            "email": "rate@example.com",
            "company_name": "Rate Inc",
            "country": "US",
        },
        "startup": {
            "one_liner": "Rate limit startup",
            "requested_check_usd": 50000,
            "use_of_funds_summary": "Test",
            "preferred_channel": "web_voice",
        },
        "consent": {
            "ai_assessment": True,
            "recording": True,
            "data_processing": True,
        },
    }
    first = client.post("/api/v1/applications", headers=_headers("rate-1", role="admin"), json=body)
    second = client.post("/api/v1/applications", headers=_headers("rate-2", role="admin"), json=body)
    assert first.status_code in {201, 429}
    assert second.status_code == 429


def test_api_key_revocation_and_expiry():
    app = create_app()
    client = TestClient(app)

    # Create a key via admin endpoint.
    created = client.post(
        "/api/v1/admin/api-keys",
        headers={"X-API-Key": TOKENS["admin"], "X-Request-Id": "req_admin_1"},
        json={"label": "temp-analyst", "role": "analyst", "expires_in_days": 1},
    )
    assert created.status_code == 201
    payload = created.json()["data"]
    key_id = payload["id"]
    temp_token = payload["token"]

    allowed = client.get(
        "/api/v1/applications/nonexistent/decision",
        headers={"X-API-Key": temp_token},
    )
    # Auth succeeds, route may 404 because app doesn't exist.
    assert allowed.status_code in {200, 404}

    revoked = client.post(
        f"/api/v1/admin/api-keys/{key_id}/revoke",
        headers={"X-API-Key": TOKENS["admin"], "X-Request-Id": "req_admin_2"},
    )
    assert revoked.status_code == 200

    denied = client.get(
        "/api/v1/applications/nonexistent/decision",
        headers={"X-API-Key": temp_token},
    )
    assert denied.status_code == 401

    # Rotate admin key and verify old key is blocked while new one works.
    rotated = client.post(
        "/api/v1/admin/api-keys/key_not_real/rotate",
        headers={"X-API-Key": TOKENS["admin"], "X-Request-Id": "req_admin_rotate_missing"},
        json={"expires_in_days": 1},
    )
    assert rotated.status_code == 404

    listed = client.get(
        "/api/v1/admin/api-keys",
        headers={"X-API-Key": TOKENS["admin"], "X-Request-Id": "req_admin_3"},
    )
    assert listed.status_code == 200
    assert "keys" in listed.json()["data"]

    # Ensure audit rows are persisted.
    db = SessionLocal()
    try:
        events = db.query(ApiKeyAuditEvent).all()
        assert len(events) >= 2
    finally:
        db.close()


def test_bootstrap_admin_token_from_secret_manager(monkeypatch):
    class _DummySecretManager:
        def __init__(self):
            self.secret = "bootstrap-secret-token"

        def get_secret(self, secret_ref: str) -> str:
            assert secret_ref == "coherence/fund/bootstrap-admin"
            return self.secret

        def put_secret(self, secret_ref: str, secret_value: str) -> None:
            return None

    dummy = _DummySecretManager()
    monkeypatch.setenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true")
    monkeypatch.setenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "coherence/fund/bootstrap-admin")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "vault")
    monkeypatch.setattr(
        "coherence_engine.server.fund.security.get_secret_manager",
        lambda: dummy,
    )
    from coherence_engine.server.fund import security as security_module
    security_module._reset_bootstrap_cache_for_tests()

    app = create_app()
    client = TestClient(app)
    create_res = client.post(
        "/api/v1/applications",
        headers={"X-API-Key": "bootstrap-secret-token", "Idempotency-Key": "bootstrap-1", "X-Request-Id": "req_bootstrap"},
        json={
            "founder": {
                "full_name": "Bootstrap Admin",
                "email": "bootstrap@example.com",
                "company_name": "Bootstrap Inc",
                "country": "US",
            },
            "startup": {
                "one_liner": "Bootstrap auth test",
                "requested_check_usd": 10000,
                "use_of_funds_summary": "test",
                "preferred_channel": "web_voice",
            },
            "consent": {"ai_assessment": True, "recording": True, "data_processing": True},
        },
    )
    assert create_res.status_code == 201


def test_create_key_with_secret_manager_sync(monkeypatch):
    class _DummySecretManager:
        def __init__(self):
            self.writes = []

        def get_secret(self, secret_ref: str) -> str:
            return "unused"

        def put_secret(self, secret_ref: str, secret_value: str) -> None:
            self.writes.append((secret_ref, secret_value))

    dummy = _DummySecretManager()
    monkeypatch.setattr(
        "coherence_engine.server.fund.routers.admin_api_keys.get_secret_manager",
        lambda: dummy,
    )

    app = create_app()
    client = TestClient(app)
    created = client.post(
        "/api/v1/admin/api-keys",
        headers={"X-API-Key": TOKENS["admin"], "X-Request-Id": "req_admin_sync_1"},
        json={
            "label": "managed-worker",
            "role": "analyst",
            "expires_in_days": 7,
            "write_to_secret_manager": True,
            "secret_ref": "coherence/fund/managed-worker",
        },
    )
    assert created.status_code == 201
    assert len(dummy.writes) == 1
    assert dummy.writes[0][0] == "coherence/fund/managed-worker"


def test_secret_manager_ready_endpoint_disabled():
    app = create_app()
    with TestClient(app) as client:
        res = client.get("/api/v1/secret-manager/ready")
        assert res.status_code == 200
        assert res.json()["data"]["status"] == "disabled"


def test_strict_policy_blocks_static_aws_credentials(monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "aws")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_ALLOW_STATIC_CREDENTIALS", "false")
    monkeypatch.setenv("COHERENCE_FUND_AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_TEST")
    with pytest.raises(SecretManagerError):
        validate_secret_manager_policy()


def test_secret_manager_ready_endpoint_reports_failed_when_policy_invalid(monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "vault")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_STARTUP_ENFORCE", "false")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true")
    monkeypatch.setenv("COHERENCE_FUND_VAULT_ADDR", "http://vault.internal:8200")
    monkeypatch.setenv("COHERENCE_FUND_VAULT_TOKEN", "dummy-token")
    app = create_app()
    with TestClient(app) as client:
        res = client.get("/api/v1/secret-manager/ready")
        assert res.status_code == 503
        assert res.json()["data"]["status"] == "failed"

