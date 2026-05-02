"""End-to-end FastAPI tests for ``security.api_key_auth`` (prompt 28).

Builds a tiny ad-hoc app that mounts ``require_scopes(...)`` deps on
isolated routes and asserts the public contract: scope subset
enforcement, distinct error codes for expired / revoked / missing
keys, and 429 on per-key rate-limit exhaustion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("argon2")
pytest.importorskip("fastapi")

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.security.api_key_auth import (
    RATE_LIMITER,
    require_scopes,
)
from coherence_engine.server.fund.services.api_key_service import ApiKeyService


@pytest.fixture(autouse=True)
def reset_db_and_limiter():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    RATE_LIMITER.reset()
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    RATE_LIMITER.reset()


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/worker/claim")
    def claim(_=Depends(require_scopes("worker:claim"))):
        return {"ok": True}

    @app.get("/worker/complete")
    def complete(_=Depends(require_scopes("worker:claim", "worker:complete"))):
        return {"ok": True}

    @app.get("/admin/things")
    def admin_things(_=Depends(require_scopes("admin:write"))):
        return {"ok": True}

    return app


def _create_key(scopes, *, rate_limit=60, expires_at=None, revoked=False, account_name="acct"):
    db = SessionLocal()
    try:
        sa = (
            db.query(models.ServiceAccount)
            .filter(models.ServiceAccount.name == account_name)
            .one_or_none()
        )
        if sa is None:
            sa = models.ServiceAccount(id=f"sa_{account_name}", name=account_name)
            db.add(sa)
            db.flush()
        svc = ApiKeyService()
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=scopes,
            created_by="test",
            expires_at=expires_at,
            rate_limit_per_minute=rate_limit,
        )
        if revoked:
            svc.revoke_key_v2(db, created.prefix)
        db.commit()
        return created
    finally:
        db.close()


def test_scope_subset_enforcement_allows_superset():
    app = _build_app()
    client = TestClient(app)
    key = _create_key(["worker:claim", "worker:complete", "applications:read"])
    res = client.get("/worker/claim", headers={"X-API-Key": key.token})
    assert res.status_code == 200, res.text


def test_scope_subset_enforcement_rejects_missing_scope():
    app = _build_app()
    client = TestClient(app)
    key = _create_key(["worker:claim"])
    res = client.get("/worker/complete", headers={"X-API-Key": key.token})
    assert res.status_code == 403, res.text
    body = res.json()
    detail = body.get("detail") or {}
    assert detail.get("code") == "INSUFFICIENT_SCOPE"
    missing = (detail.get("details") or {}).get("missing") or []
    assert "worker:complete" in missing


def test_admin_route_requires_admin_write_scope():
    app = _build_app()
    client = TestClient(app)
    analyst = _create_key(["applications:read", "applications:write"])
    res = client.get("/admin/things", headers={"X-API-Key": analyst.token})
    assert res.status_code == 403
    admin = _create_key(["admin:write", "admin:read"], account_name="ops")
    res = client.get("/admin/things", headers={"X-API-Key": admin.token})
    assert res.status_code == 200


def test_missing_token_returns_401_unauthorized():
    app = _build_app()
    client = TestClient(app)
    res = client.get("/worker/claim")
    assert res.status_code == 401
    body = res.json()
    assert (body.get("detail") or {}).get("code") == "UNAUTHORIZED"


def test_expired_key_returns_401_with_distinct_code():
    app = _build_app()
    client = TestClient(app)
    past = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
    key = _create_key(["worker:claim"], expires_at=past)
    res = client.get("/worker/claim", headers={"X-API-Key": key.token})
    assert res.status_code == 401
    assert (res.json().get("detail") or {}).get("code") == "UNAUTHORIZED_EXPIRED"


def test_revoked_key_returns_401_with_distinct_code():
    app = _build_app()
    client = TestClient(app)
    key = _create_key(["worker:claim"], revoked=True)
    res = client.get("/worker/claim", headers={"X-API-Key": key.token})
    assert res.status_code == 401
    assert (res.json().get("detail") or {}).get("code") == "UNAUTHORIZED_REVOKED"


def test_authorization_bearer_header_also_accepted():
    app = _build_app()
    client = TestClient(app)
    key = _create_key(["worker:claim"])
    res = client.get(
        "/worker/claim",
        headers={"Authorization": f"Bearer {key.token}"},
    )
    assert res.status_code == 200


def test_rate_limit_exhaustion_returns_429():
    app = _build_app()
    client = TestClient(app)
    key = _create_key(["worker:claim"], rate_limit=3)
    headers = {"X-API-Key": key.token}
    successes = 0
    saw_429 = False
    for _ in range(20):
        res = client.get("/worker/claim", headers=headers)
        if res.status_code == 200:
            successes += 1
        elif res.status_code == 429:
            saw_429 = True
            body = res.json()
            assert (body.get("detail") or {}).get("code") == "RATE_LIMITED"
            break
        else:
            pytest.fail(f"unexpected status {res.status_code}: {res.text}")
    assert successes >= 1
    assert saw_429, "expected RATE_LIMITED before 20 requests at limit=3/min"
