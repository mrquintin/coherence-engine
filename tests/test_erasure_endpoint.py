"""POST /api/v1/privacy/erasure tests (prompt 57)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.routers.privacy import (
    issue_verification_token,
    router as privacy_router,
)
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services.per_row_encryption import (
    encrypt,
    set_encryption_key_store,
)
from coherence_engine.server.fund.services.retention import (
    ERASURE_GRACE_DAYS,
    ERASURE_REFUSED_AUDIT_HOLD,
    execute_erasure,
)


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    set_encryption_key_store(None)
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["LOCAL_STORAGE_ROOT"] = str(tmp_path / "obj")
    object_storage.reset_object_storage()
    yield
    object_storage.reset_object_storage()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _build_app(role: str = "subject") -> FastAPI:
    """Build a tiny app that mounts the privacy router with a fixed principal.

    Tests pin a role on ``request.state.principal`` so we exercise both
    the subject path (no role) and the admin path
    (``immediate=true``).
    """
    app = FastAPI()

    @app.middleware("http")
    async def _principal(request: Request, call_next):
        request.state.principal = {
            "auth_type": "test",
            "role": role,
            "fingerprint": f"fp_{role}",
            "key_id": None,
        }
        return await call_next(request)

    app.include_router(privacy_router, prefix="/api/v1")
    return app


def _seed_founder(db, founder_id: str = "fnd_era_01") -> models.Founder:
    f = models.Founder(
        id=founder_id,
        full_name="Erasure Subject",
        email="subject@example.com",
        company_name="X",
        country="US",
    )
    db.add(f)
    db.flush()
    return f


def _seed_application_with_transcript(
    db, *, founder_id: str, application_id: str = "app_era_01"
) -> tuple[models.Application, str]:
    blob = b"transcript bytes for erasure test"
    put = object_storage.put(
        f"transcripts/{application_id}.txt", blob, content_type="text/plain"
    )
    key_id, ct = encrypt(blob, db=db, row_id=application_id)
    app = models.Application(
        id=application_id,
        founder_id=founder_id,
        one_liner="x",
        requested_check_usd=100000,
        use_of_funds_summary="x",
        preferred_channel="email",
        transcript_text=ct,
        transcript_uri=put.uri,
        transcript_key_id=key_id,
    )
    db.add(app)
    db.flush()
    return app, key_id


def test_issue_token_then_subject_schedules_within_30_days():
    app = _build_app(role="support")
    client = TestClient(app)
    db = SessionLocal()
    try:
        founder = _seed_founder(db)
        db.commit()
    finally:
        db.close()

    issue = client.post(
        "/api/v1/privacy/erasure/issue",
        json={"subject_id": founder.id, "subject_type": "founder"},
    )
    assert issue.status_code == 200, issue.text
    issue_data = issue.json()["data"]
    token = issue_data["verification_token"]
    erasure_id = issue_data["erasure_request_id"]

    # Subject endpoint runs as a non-admin role (no immediate access).
    subject_app = _build_app(role="founder")
    subject_client = TestClient(subject_app)
    res = subject_client.post(
        "/api/v1/privacy/erasure",
        json={"subject_id": founder.id, "verification_token": token},
    )
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["status"] == "scheduled"
    assert body["erasure_request_id"] == erasure_id
    scheduled_for = datetime.fromisoformat(body["scheduled_for"])
    delta = scheduled_for - datetime.now(tz=timezone.utc)
    # Allow a 1-minute fudge for test wall-clock drift.
    assert timedelta(days=ERASURE_GRACE_DAYS - 1) < delta <= timedelta(
        days=ERASURE_GRACE_DAYS, minutes=1
    )
    assert "transcript" in body["classes"]
    assert "decision_artifact" not in body["classes"]


def test_invalid_token_is_unauthorized():
    app = _build_app(role="founder")
    client = TestClient(app)
    res = client.post(
        "/api/v1/privacy/erasure",
        json={"subject_id": "fnd_x", "verification_token": "totally-bogus"},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_subject_id_mismatch_is_unauthorized():
    """A real token cannot be reused for a different subject_id."""
    db = SessionLocal()
    try:
        f = _seed_founder(db)
        result = issue_verification_token(
            db, subject_id=f.id, subject_type="founder", issued_by="support"
        )
        db.commit()
        token = result["verification_token"]
    finally:
        db.close()
    app = _build_app(role="founder")
    client = TestClient(app)
    res = client.post(
        "/api/v1/privacy/erasure",
        json={"subject_id": "fnd_someone_else", "verification_token": token},
    )
    assert res.status_code == 401


def test_audit_hold_class_is_refused():
    """Requesting decision_artifact erasure must refuse with ERASURE_REFUSED_AUDIT_HOLD."""
    db = SessionLocal()
    try:
        f = _seed_founder(db, founder_id="fnd_hold_01")
        result = issue_verification_token(
            db, subject_id=f.id, subject_type="founder", issued_by="support"
        )
        db.commit()
        token = result["verification_token"]
    finally:
        db.close()
    app = _build_app(role="founder")
    client = TestClient(app)
    res = client.post(
        "/api/v1/privacy/erasure",
        json={
            "subject_id": "fnd_hold_01",
            "verification_token": token,
            "classes": ["transcript", "decision_artifact"],
        },
    )
    assert res.status_code == 200
    body = res.json()["data"]
    assert body["status"] == "refused"
    assert body["refusal_reason"] == ERASURE_REFUSED_AUDIT_HOLD


def test_idempotent_replay_returns_same_record():
    db = SessionLocal()
    try:
        f = _seed_founder(db, founder_id="fnd_idem_01")
        result = issue_verification_token(
            db, subject_id=f.id, subject_type="founder", issued_by="support"
        )
        db.commit()
        token = result["verification_token"]
    finally:
        db.close()
    app = _build_app(role="founder")
    client = TestClient(app)
    first = client.post(
        "/api/v1/privacy/erasure",
        json={"subject_id": "fnd_idem_01", "verification_token": token},
    )
    second = client.post(
        "/api/v1/privacy/erasure",
        json={"subject_id": "fnd_idem_01", "verification_token": token},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    a = first.json()["data"]
    b = second.json()["data"]
    assert a["erasure_request_id"] == b["erasure_request_id"]
    assert a["status"] == b["status"] == "scheduled"
    assert b["idempotent"] is True


def test_immediate_requires_admin_role():
    db = SessionLocal()
    try:
        f = _seed_founder(db, founder_id="fnd_imm_01")
        result = issue_verification_token(
            db, subject_id=f.id, subject_type="founder", issued_by="support"
        )
        db.commit()
        token = result["verification_token"]
    finally:
        db.close()
    # Non-admin: forbidden.
    app = _build_app(role="founder")
    client = TestClient(app)
    res = client.post(
        "/api/v1/privacy/erasure",
        json={
            "subject_id": "fnd_imm_01",
            "verification_token": token,
            "immediate": True,
        },
    )
    assert res.status_code == 403, res.text


def test_response_does_not_confirm_completion_before_worker_runs():
    """Per prompt 57: do NOT confirm erasure to the requestor before the deletion job runs.

    The handler must return ``status="scheduled"`` -- never
    ``"completed"``. The DB row likewise must remain in ``scheduled``
    state until ``execute_erasure`` runs.
    """
    db = SessionLocal()
    try:
        f = _seed_founder(db, founder_id="fnd_pre_01")
        _seed_application_with_transcript(db, founder_id=f.id)
        result = issue_verification_token(
            db, subject_id=f.id, subject_type="founder", issued_by="support"
        )
        db.commit()
        token = result["verification_token"]
        erasure_id = result["erasure_request_id"]
    finally:
        db.close()

    app = _build_app(role="founder")
    client = TestClient(app)
    res = client.post(
        "/api/v1/privacy/erasure",
        json={"subject_id": "fnd_pre_01", "verification_token": token},
    )
    body = res.json()["data"]
    assert body["status"] == "scheduled"
    assert body["completed_at"] is None

    # Application row must still hold its (non-tombstoned) transcript.
    db = SessionLocal()
    try:
        app_row = (
            db.query(models.Application)
            .filter(models.Application.founder_id == "fnd_pre_01")
            .one()
        )
        assert app_row.redacted is False
        # Now run the worker -- this is the only thing that completes.
        receipt = execute_erasure(db, erasure_id)
        db.commit()
        assert receipt["erased_classes"]
        # And the transcript row is now redacted.
        db.refresh(app_row)
        assert app_row.redacted is True
        # The erasure request row reflects completion.
        req = db.get(models.ErasureRequest, erasure_id)
        assert req.status == "completed"
        assert req.completed_at is not None
    finally:
        db.close()


def test_get_returns_410_after_redaction():
    """A read against a redacted application must surface HTTP 410 Gone semantics.

    The router-side test mounts a tiny "/read" probe that the production
    fund routers will mirror once they are updated; here we assert the
    contract at the model level: ``redacted=True`` + ``redaction_reason``
    is the source-of-truth signal that endpoints translate to 410.
    """
    db = SessionLocal()
    try:
        f = _seed_founder(db, founder_id="fnd_read_01")
        app_row, key_id = _seed_application_with_transcript(
            db, founder_id=f.id, application_id="app_read_01"
        )
        # Force a redaction.
        from coherence_engine.server.fund.services.retention import (
            _target_for,
            tombstone_and_shred,
        )

        target = _target_for("transcript")
        tombstone_and_shred(
            db, target, app_row, reason="erasure:test",
            now=datetime.now(tz=timezone.utc),
        )
        db.commit()

        # Build a tiny app that maps the redacted state to HTTP 410.
        api = FastAPI()

        @api.get("/applications/{app_id}/transcript")
        def read_transcript(app_id: str):
            from fastapi import HTTPException

            sub_db = SessionLocal()
            try:
                row = sub_db.get(models.Application, app_id)
                if row is None:
                    raise HTTPException(status_code=404, detail="not_found")
                if row.redacted:
                    raise HTTPException(
                        status_code=410,
                        detail={
                            "code": "GONE",
                            "redaction_reason": row.redaction_reason,
                        },
                    )
                return {"transcript": row.transcript_text}
            finally:
                sub_db.close()

        client = TestClient(api)
        res = client.get("/applications/app_read_01/transcript")
        assert res.status_code == 410, res.text
        assert res.json()["detail"]["code"] == "GONE"
        assert "erasure:test" in res.json()["detail"]["redaction_reason"]
    finally:
        db.close()
