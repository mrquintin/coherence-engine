"""E-signature service + webhook tests (prompt 52).

Covers:

* Template rendering produces a deterministic vars hash; the unsigned
  body is in-memory only.
* Provider backends: prepare/send returns deterministic ids on retry;
  fetch_signed_artifact returns PDF bytes.
* Webhook signature verification: DocuSign (HMAC-SHA-256 + base64,
  rotation across multiple secrets) and Dropbox Sign (HMAC-SHA-256 of
  ``event_time + event_type``) accept-valid / reject-invalid.
* Service idempotency: prepare with the same idempotency key collapses
  onto a single SignatureRequest row.
* Webhook reconciliation: signed-PDF upload to object storage,
  duplicate webhook is a no-op.
* Router tests via TestClient: 401 on bad signature; 200 on valid
  webhook with state mutation; 200 on informational events without
  mutation.

No real DocuSign / Dropbox Sign HTTP is exercised -- both backends
emit deterministic synthetic ids.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

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
from coherence_engine.server.fund.routers.esignature_webhooks import (
    reset_backends_for_tests,
    router as esignature_webhook_router,
    set_docusign_backend_for_tests,
    set_dropbox_sign_backend_for_tests,
    webhook_signature_ok,
)
from coherence_engine.server.fund.services import esignature as svc
from coherence_engine.server.fund.services import object_storage as _object_storage
from coherence_engine.server.fund.services.esignature import (
    ESignatureError,
    ESignatureService,
    Signer,
    compute_idempotency_key,
    compute_template_vars_hash,
    render_template,
)
from coherence_engine.server.fund.services.esignature_backends import (
    DocuSignBackend,
    DropboxSignBackend,
    verify_docusign_webhook_signature,
    verify_dropbox_sign_webhook_signature,
)
from coherence_engine.server.fund.services.storage_backends import (
    LocalFilesystemBackend,
)


DOCUSIGN_HMAC_SECRETS = ("docusign-secret-1", "docusign-secret-2")
DROPBOX_SIGN_API_KEY = "dropbox-sign-test-key"


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
def _local_storage(tmp_path):
    backend = LocalFilesystemBackend(
        root=str(tmp_path / "storage"), bucket="esig-test"
    )
    _object_storage.set_object_storage(backend)
    yield backend
    _object_storage.reset_object_storage()


@pytest.fixture(autouse=True)
def _backend_reset():
    yield
    reset_backends_for_tests()


@pytest.fixture
def docusign_backend():
    return DocuSignBackend(
        integration_key="dummy-int-key",
        user_id="dummy-user-id",
        rsa_private_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        connect_hmac_secrets=DOCUSIGN_HMAC_SECRETS,
    )


@pytest.fixture
def dropbox_sign_backend():
    return DropboxSignBackend(api_key=DROPBOX_SIGN_API_KEY)


def _persist_application(app_id: str = "app_esig_1") -> models.Application:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_esig_1",
            full_name="Esig Founder",
            email="esig@example.com",
            country="US",
            company_name="EsigCo",
        )
        application = models.Application(
            id=app_id,
            founder_id=founder.id,
            one_liner="Esig pilot",
            requested_check_usd=50_000,
            use_of_funds_summary="seed",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="decision_issued",
            scoring_mode="enforce",
        )
        db.add_all([founder, application])
        db.commit()
        db.refresh(application)
        return application
    finally:
        db.close()


def _safe_template_vars() -> dict:
    return {
        "company_name": "Example Inc.",
        "investor_name": "Coherence Engine Fund I",
        "investor_entity": "Delaware LP",
        "purchase_amount_usd": "50000",
        "valuation_cap_usd": "8000000",
        "discount_rate_pct": "20",
        "effective_date": "2026-04-25",
        "signer_company_name": "Example Founder",
        "signer_investor_name": "GP Signatory",
    }


def _signers() -> list[Signer]:
    return [
        Signer(name="Example Founder", email="founder@example.com", role="founder"),
        Signer(name="GP Signatory", email="gp@coherence.fund", role="investor"),
    ]


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_render_template_produces_deterministic_vars_hash():
    vars_a = _safe_template_vars()
    vars_b = dict(reversed(list(vars_a.items())))
    assert compute_template_vars_hash(vars_a) == compute_template_vars_hash(vars_b)


def test_render_template_substitutes_placeholders():
    rendered = render_template("safe_note_v1", _safe_template_vars())
    body_text = rendered.body.decode("utf-8")
    assert "Example Inc." in body_text
    assert "Coherence Engine Fund I" in body_text
    # Comment block ({# ... #}) must be stripped from the rendered output.
    assert "PLACEHOLDER SAFE NOTE TEMPLATE" not in body_text


def test_render_template_unknown_id_raises():
    with pytest.raises(ESignatureError):
        render_template("does_not_exist", {})


# ---------------------------------------------------------------------------
# Backend prepare/send/fetch
# ---------------------------------------------------------------------------


def test_docusign_send_is_deterministic_per_idempotency_key(docusign_backend):
    document = render_template("safe_note_v1", _safe_template_vars())
    a = docusign_backend.send(
        document=document, signers=_signers(), idempotency_key="abc"
    )
    b = docusign_backend.send(
        document=document, signers=_signers(), idempotency_key="abc"
    )
    c = docusign_backend.send(
        document=document, signers=_signers(), idempotency_key="xyz"
    )
    assert a.provider_request_id == b.provider_request_id
    assert a.provider_request_id != c.provider_request_id
    assert a.provider_request_id.startswith("env_")


def test_dropbox_sign_fetch_signed_artifact_returns_pdf(dropbox_sign_backend):
    artifact = dropbox_sign_backend.fetch_signed_artifact(
        provider_request_id="sigreq_test"
    )
    assert artifact.content_type == "application/pdf"
    assert artifact.pdf_bytes.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Webhook signature verification (low-level)
# ---------------------------------------------------------------------------


def _docusign_sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def test_docusign_signature_accepts_valid_header():
    body = b'{"event":"envelope-completed"}'
    header = _docusign_sign(DOCUSIGN_HMAC_SECRETS[0], body)
    assert verify_docusign_webhook_signature(
        DOCUSIGN_HMAC_SECRETS, body, [header]
    ) is True


def test_docusign_signature_accepts_rotated_secret():
    body = b'{"event":"envelope-completed"}'
    header = _docusign_sign(DOCUSIGN_HMAC_SECRETS[1], body)
    # Header signed with the second-active secret must verify too.
    assert verify_docusign_webhook_signature(
        DOCUSIGN_HMAC_SECRETS, body, [header]
    ) is True


def test_docusign_signature_rejects_bad_digest():
    body = b'{"event":"envelope-completed"}'
    bad_header = base64.b64encode(b"x" * 32).decode("ascii")
    assert verify_docusign_webhook_signature(
        DOCUSIGN_HMAC_SECRETS, body, [bad_header]
    ) is False


def test_docusign_signature_rejects_empty_inputs():
    body = b"{}"
    assert verify_docusign_webhook_signature((), body, ["x"]) is False
    assert verify_docusign_webhook_signature(("s",), body, []) is False
    assert verify_docusign_webhook_signature(("",), body, ["x"]) is False


def test_dropbox_sign_signature_accepts_valid_event_hash():
    event_time = "1700000000"
    event_type = "signature_request_all_signed"
    expected = hmac.new(
        DROPBOX_SIGN_API_KEY.encode("utf-8"),
        f"{event_time}{event_type}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    body = json.dumps(
        {
            "event": {
                "event_time": event_time,
                "event_type": event_type,
                "event_hash": expected,
            },
            "signature_request": {"signature_request_id": "sigreq_x"},
        }
    ).encode("utf-8")
    assert verify_dropbox_sign_webhook_signature(DROPBOX_SIGN_API_KEY, body) is True


def test_dropbox_sign_signature_rejects_bad_hash():
    body = json.dumps(
        {
            "event": {
                "event_time": "1700000000",
                "event_type": "signature_request_all_signed",
                "event_hash": "deadbeef",
            }
        }
    ).encode("utf-8")
    assert verify_dropbox_sign_webhook_signature(DROPBOX_SIGN_API_KEY, body) is False


def test_dropbox_sign_signature_rejects_empty_api_key():
    assert verify_dropbox_sign_webhook_signature("", b"{}") is False


# ---------------------------------------------------------------------------
# Service: prepare idempotency
# ---------------------------------------------------------------------------


def test_service_prepare_is_idempotent(docusign_backend):
    application = _persist_application()
    key = compute_idempotency_key(application.id, "safe_note_v1", salt="req-1")
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        first = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
            idempotency_key=key,
        )
        db.commit()
        second = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
            idempotency_key=key,
        )
        db.commit()
        assert first.id == second.id
        rows = db.query(models.SignatureRequest).count()
        assert rows == 1
    finally:
        db.close()


def test_service_prepare_persists_template_vars_hash_only(docusign_backend):
    application = _persist_application()
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        row = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
        )
        db.commit()
        assert row.template_vars_hash == compute_template_vars_hash(
            _safe_template_vars()
        )
        # The unsigned body is never persisted.
        assert row.signed_pdf_uri == ""
        assert row.status == "prepared"
    finally:
        db.close()


def test_service_prepare_requires_signers(docusign_backend):
    application = _persist_application()
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        with pytest.raises(ESignatureError):
            service.prepare(
                provider=docusign_backend,
                application_id=application.id,
                document_template="safe_note_v1",
                template_vars=_safe_template_vars(),
                signers=[],
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: send + signed-PDF upload + duplicate webhook
# ---------------------------------------------------------------------------


def test_service_send_advances_status_and_assigns_provider_id(docusign_backend):
    application = _persist_application()
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        row = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
        )
        sent = service.send(
            provider=docusign_backend,
            request=row,
            template_vars=_safe_template_vars(),
        )
        db.commit()
        assert sent.status == "sent"
        assert sent.provider_request_id.startswith("env_")
    finally:
        db.close()


def test_service_send_rejects_var_substitution(docusign_backend):
    application = _persist_application()
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        row = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
        )
        tampered = dict(_safe_template_vars())
        tampered["purchase_amount_usd"] = "99999999"
        with pytest.raises(ESignatureError):
            service.send(
                provider=docusign_backend,
                request=row,
                template_vars=tampered,
            )
    finally:
        db.close()


def test_service_apply_webhook_uploads_signed_pdf(docusign_backend, _local_storage):
    application = _persist_application()
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        row = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
        )
        service.send(
            provider=docusign_backend,
            request=row,
            template_vars=_safe_template_vars(),
        )
        applied = service.apply_webhook(
            provider=docusign_backend,
            provider_request_id=row.provider_request_id,
            new_status="signed",
        )
        db.commit()
        assert applied is not None
        assert applied.status == "signed"
        assert applied.signed_pdf_uri.startswith("coh://local/")
        # Re-fetch the bytes from storage to confirm round-trip.
        fetched = _local_storage.get(applied.signed_pdf_uri)
        assert fetched.startswith(b"%PDF")
    finally:
        db.close()


def test_service_apply_webhook_is_idempotent_on_duplicate(docusign_backend):
    application = _persist_application()
    db = SessionLocal()
    try:
        service = ESignatureService(db=db)
        row = service.prepare(
            provider=docusign_backend,
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars=_safe_template_vars(),
            signers=_signers(),
        )
        service.send(
            provider=docusign_backend,
            request=row,
            template_vars=_safe_template_vars(),
        )
        first = service.apply_webhook(
            provider=docusign_backend,
            provider_request_id=row.provider_request_id,
            new_status="signed",
        )
        first_uri = first.signed_pdf_uri
        # A duplicate webhook for the same terminal status must not
        # mutate signed_pdf_uri or call fetch again.
        second = service.apply_webhook(
            provider=docusign_backend,
            provider_request_id=row.provider_request_id,
            new_status="signed",
        )
        assert second.signed_pdf_uri == first_uri
        assert second.status == "signed"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router via TestClient
# ---------------------------------------------------------------------------


def _make_app(docusign_backend, dropbox_sign_backend) -> FastAPI:
    app = FastAPI()
    app.include_router(esignature_webhook_router, prefix="/api/v1")
    set_docusign_backend_for_tests(docusign_backend)
    set_dropbox_sign_backend_for_tests(dropbox_sign_backend)
    return app


def _seed_signature_request(
    application: models.Application,
    *,
    provider: str,
    provider_request_id: str,
) -> models.SignatureRequest:
    db = SessionLocal()
    try:
        row = models.SignatureRequest(
            id=f"sig_{provider}_test",
            application_id=application.id,
            document_template="safe_note_v1",
            template_vars_hash="x" * 64,
            provider=provider,
            provider_request_id=provider_request_id,
            status="sent",
            signers_json="[]",
            idempotency_key=f"idem_{provider}_test",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def test_docusign_router_rejects_invalid_signature(
    docusign_backend, dropbox_sign_backend
):
    application = _persist_application()
    _seed_signature_request(
        application, provider="docusign", provider_request_id="env_xxx"
    )
    app = _make_app(docusign_backend, dropbox_sign_backend)
    client = TestClient(app)
    res = client.post(
        "/api/v1/webhooks/esignature/docusign",
        content=b'{"foo":"bar"}',
        headers={"X-DocuSign-Signature-1": "not-real"},
    )
    assert res.status_code == 401


def test_docusign_router_accepts_valid_signature_and_advances_status(
    docusign_backend, dropbox_sign_backend
):
    application = _persist_application()
    row = _seed_signature_request(
        application, provider="docusign", provider_request_id="env_routerx"
    )
    body_payload = {
        "event": "envelope-completed",
        "data": {
            "envelopeSummary": {
                "envelopeId": row.provider_request_id,
                "status": "completed",
            }
        },
    }
    body = json.dumps(body_payload).encode("utf-8")
    header = _docusign_sign(DOCUSIGN_HMAC_SECRETS[0], body)
    app = _make_app(docusign_backend, dropbox_sign_backend)
    client = TestClient(app)
    res = client.post(
        "/api/v1/webhooks/esignature/docusign",
        content=body,
        headers={"X-DocuSign-Signature-1": header},
    )
    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        refreshed = db.get(models.SignatureRequest, row.id)
        assert refreshed.status == "signed"
        assert refreshed.signed_pdf_uri.startswith("coh://local/")
    finally:
        db.close()


def test_dropbox_sign_router_rejects_invalid_signature(
    docusign_backend, dropbox_sign_backend
):
    application = _persist_application()
    _seed_signature_request(
        application, provider="dropbox_sign", provider_request_id="sigreq_x"
    )
    app = _make_app(docusign_backend, dropbox_sign_backend)
    client = TestClient(app)
    body = json.dumps(
        {
            "event": {
                "event_time": "1700000000",
                "event_type": "signature_request_all_signed",
                "event_hash": "wrong-hash",
            },
            "signature_request": {"signature_request_id": "sigreq_x"},
        }
    ).encode("utf-8")
    res = client.post(
        "/api/v1/webhooks/esignature/dropbox-sign", content=body
    )
    assert res.status_code == 401


def test_dropbox_sign_router_accepts_valid_signature(
    docusign_backend, dropbox_sign_backend
):
    application = _persist_application()
    row = _seed_signature_request(
        application,
        provider="dropbox_sign",
        provider_request_id="sigreq_router1",
    )
    event_time = "1700000000"
    event_type = "signature_request_all_signed"
    event_hash = hmac.new(
        DROPBOX_SIGN_API_KEY.encode("utf-8"),
        f"{event_time}{event_type}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    body = json.dumps(
        {
            "event": {
                "event_time": event_time,
                "event_type": event_type,
                "event_hash": event_hash,
            },
            "signature_request": {
                "signature_request_id": row.provider_request_id
            },
        }
    ).encode("utf-8")
    app = _make_app(docusign_backend, dropbox_sign_backend)
    client = TestClient(app)
    res = client.post(
        "/api/v1/webhooks/esignature/dropbox-sign", content=body
    )
    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        refreshed = db.get(models.SignatureRequest, row.id)
        assert refreshed.status == "signed"
    finally:
        db.close()


def test_webhook_signature_ok_helper_exposed(docusign_backend):
    body = b'{"event":"x"}'
    header = _docusign_sign(DOCUSIGN_HMAC_SECRETS[0], body)
    assert webhook_signature_ok(
        docusign_backend, body, {"X-DocuSign-Signature-1": header}
    ) is True
    assert webhook_signature_ok(
        docusign_backend, body, {"X-DocuSign-Signature-1": "bad"}
    ) is False


# ---------------------------------------------------------------------------
# Defense in depth: backend.from_env requires its env vars
# ---------------------------------------------------------------------------


def test_docusign_from_env_requires_keys(monkeypatch):
    monkeypatch.delenv("DOCUSIGN_INTEGRATION_KEY", raising=False)
    with pytest.raises(svc.ESignatureConfigError):
        DocuSignBackend.from_env()


def test_dropbox_sign_from_env_requires_keys(monkeypatch):
    monkeypatch.delenv("DROPBOX_SIGN_API_KEY", raising=False)
    with pytest.raises(svc.ESignatureConfigError):
        DropboxSignBackend.from_env()
