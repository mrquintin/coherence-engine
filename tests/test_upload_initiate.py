"""Tests for the founder-portal upload signed-URL flow.

Covers ``POST /applications/{id}/uploads:initiate`` (signed URL, expiry,
key prefix) and ``POST /applications/{id}/uploads:complete`` (idempotency
and server-side size verification — never trusts the client-supplied
size_bytes).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from coherence_engine.server.fund.app import create_app
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services.api_key_service import ApiKeyService
from coherence_engine.server.fund.services.storage_backends import LocalFilesystemBackend


TOKENS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def reset_state(tmp_path):
    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        analyst = svc.create_key(
            repo,
            label="upload-test-analyst",
            role="analyst",
            created_by="tests",
            expires_in_days=30,
        )
        TOKENS["analyst"] = analyst["token"]
        db.commit()
    finally:
        db.close()

    # Inject a clean local backend pointed at tmp_path so tests don't pollute
    # the repo working tree and each test starts from an empty bucket.
    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="default")
    object_storage.set_object_storage(backend)

    yield

    object_storage.reset_object_storage()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _headers(idem: str) -> dict[str, str]:
    return {
        "Idempotency-Key": idem,
        "X-Request-Id": f"req_{idem}",
        "X-API-Key": TOKENS["analyst"],
    }


def _create_application(client: TestClient) -> str:
    res = client.post(
        "/api/v1/applications",
        headers=_headers("create-upload"),
        json={
            "founder": {
                "full_name": "Mara Founder",
                "email": "mara@example.com",
                "company_name": "PixelPress",
                "country": "US",
            },
            "startup": {
                "one_liner": "Self-serve press automation for indie publishers",
                "requested_check_usd": 75_000,
                "use_of_funds_summary": "Hire two engineers and ship v1",
                "preferred_channel": "web_voice",
            },
            "consent": {
                "ai_assessment": True,
                "recording": True,
                "data_processing": True,
            },
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["data"]["application_id"]


# ---------------------------------------------------------------------------
# initiate
# ---------------------------------------------------------------------------


def test_initiate_returns_signed_url_with_expiry_and_prefix():
    app = create_app()
    client = TestClient(app)
    app_id = _create_application(client)

    res = client.post(
        f"/api/v1/applications/{app_id}/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"], "X-Request-Id": "req_init"},
        json={
            "filename": "deck.pdf",
            "content_type": "application/pdf",
            "size_bytes": 4_096,
            "kind": "deck",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()["data"]

    # Signed URL contract: must surface a usable upload URL and an expiry
    # the client can sanity-check.
    assert body.get("upload_url")
    assert "expires_at" in body
    expires_at = datetime.fromisoformat(body["expires_at"])
    now = datetime.now(tz=timezone.utc)
    assert expires_at > now
    delta = (expires_at - now).total_seconds()
    # initiate sets a 600s expiry; allow a generous tolerance.
    assert 60 < delta <= 700, f"expires_at {delta}s out of range"

    # Key prefix isolates uploads per application + kind.
    assert body["key"].startswith(f"applications/{app_id}/deck/")
    assert body["uri"].startswith("coh://local/default/applications/")
    assert body["headers"].get("Content-Type") == "application/pdf"
    assert body["max_bytes"] >= 1024 * 1024


def test_initiate_rejects_disallowed_content_type():
    app = create_app()
    client = TestClient(app)
    app_id = _create_application(client)
    res = client.post(
        f"/api/v1/applications/{app_id}/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={
            "filename": "evil.exe",
            "content_type": "application/x-msdownload",
            "size_bytes": 100,
            "kind": "deck",
        },
    )
    assert res.status_code == 422


def test_initiate_rejects_oversized_payload():
    app = create_app()
    client = TestClient(app)
    app_id = _create_application(client)
    too_big = 100 * 1024 * 1024
    res = client.post(
        f"/api/v1/applications/{app_id}/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={
            "filename": "huge.pdf",
            "content_type": "application/pdf",
            "size_bytes": too_big,
            "kind": "deck",
        },
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def _put_bytes(uri: str, data: bytes) -> None:
    """Simulate the browser PUT to the signed URL by writing directly to
    the in-process storage backend (the local backend has no HTTP surface)."""
    backend = object_storage.get_object_storage()
    _, _, key = object_storage.parse_uri(uri)
    backend.put(key, data, content_type="application/pdf")


def test_complete_finalizes_after_storage_put():
    app = create_app()
    client = TestClient(app)
    app_id = _create_application(client)

    init = client.post(
        f"/api/v1/applications/{app_id}/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={
            "filename": "deck.pdf",
            "content_type": "application/pdf",
            "size_bytes": 11,  # client claims 11 bytes (lie!)
            "kind": "deck",
        },
    )
    assert init.status_code == 201
    init_body = init.json()["data"]

    # Simulate the direct PUT — client uploads 6 bytes, not 11.
    actual_bytes = b"hello!"
    _put_bytes(init_body["uri"], actual_bytes)

    res = client.post(
        f"/api/v1/applications/{app_id}/uploads:complete",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={"upload_id": init_body["upload_id"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()["data"]
    assert body["status"] == "completed"
    assert body["uri"] == init_body["uri"]
    # Server reports the storage-backed size, not the client's 11.
    assert body["size_bytes"] == len(actual_bytes)


def test_complete_is_idempotent():
    app = create_app()
    client = TestClient(app)
    app_id = _create_application(client)

    init = client.post(
        f"/api/v1/applications/{app_id}/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={
            "filename": "deck.pdf",
            "content_type": "application/pdf",
            "size_bytes": 4,
            "kind": "deck",
        },
    )
    init_body = init.json()["data"]
    _put_bytes(init_body["uri"], b"abcd")

    first = client.post(
        f"/api/v1/applications/{app_id}/uploads:complete",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={"upload_id": init_body["upload_id"]},
    )
    second = client.post(
        f"/api/v1/applications/{app_id}/uploads:complete",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={"upload_id": init_body["upload_id"]},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["uri"] == second.json()["data"]["uri"]
    assert first.json()["data"]["size_bytes"] == second.json()["data"]["size_bytes"]
    assert second.json()["data"]["status"] == "completed"


def test_complete_fails_when_object_missing():
    app = create_app()
    client = TestClient(app)
    app_id = _create_application(client)

    init = client.post(
        f"/api/v1/applications/{app_id}/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={
            "filename": "deck.pdf",
            "content_type": "application/pdf",
            "size_bytes": 4,
            "kind": "deck",
        },
    )
    upload_id = init.json()["data"]["upload_id"]

    # Skip the PUT — call complete with no object in storage.
    res = client.post(
        f"/api/v1/applications/{app_id}/uploads:complete",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={"upload_id": upload_id},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "UPLOAD_NOT_FOUND"


def test_initiate_unknown_application_returns_404():
    app = create_app()
    client = TestClient(app)
    res = client.post(
        "/api/v1/applications/app_does_not_exist/uploads:initiate",
        headers={"X-API-Key": TOKENS["analyst"]},
        json={
            "filename": "deck.pdf",
            "content_type": "application/pdf",
            "size_bytes": 100,
            "kind": "deck",
        },
    )
    assert res.status_code == 404
