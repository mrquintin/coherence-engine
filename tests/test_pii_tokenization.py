"""Tests for the PII tokenizer + Founder.read_clear_email API (prompt 58)."""

from __future__ import annotations


import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import per_row_encryption
from coherence_engine.server.fund.services.pii_clear_audit import (
    ClearReadDenied,
    ClearReadPrincipal,
    PIIClearAuditLog,
    PII_READ_CLEAR_SCOPE,
)
from coherence_engine.server.fund.services.pii_tokenization import (
    KNOWN_KINDS,
    PIITokenizationError,
    TOKEN_PREFIX,
    is_token,
    tokenize,
)


_TEST_SALT = "tests-pii-salt-not-production"


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    per_row_encryption.set_encryption_key_store(None)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _salt_env(monkeypatch):
    monkeypatch.setenv("PII_TENANT_SALT", _TEST_SALT)
    yield


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def test_tokenize_format_and_prefix():
    tok = tokenize("alice@example.com", kind="email")
    assert tok.startswith(f"{TOKEN_PREFIX}email_")
    # 32 hex chars after prefix.
    assert len(tok.split("_", 2)[2]) == 32
    assert is_token(tok)


def test_tokenize_is_deterministic_per_kind_and_salt():
    a = tokenize("alice@example.com", kind="email", tenant_salt="s1")
    b = tokenize("alice@example.com", kind="email", tenant_salt="s1")
    assert a == b, "same (value, kind, salt) must yield same token"


def test_tokenize_changes_with_salt():
    a = tokenize("alice@example.com", kind="email", tenant_salt="s1")
    b = tokenize("alice@example.com", kind="email", tenant_salt="s2")
    assert a != b, "different salts must yield different tokens"


def test_tokenize_changes_with_kind():
    # Same value across different kinds must NOT collide.
    a = tokenize("123-456-7890", kind="phone", tenant_salt="s1")
    b = tokenize("123-456-7890", kind="address", tenant_salt="s1")
    assert a != b


def test_tokenize_normalizes_email_case_and_whitespace():
    a = tokenize("Alice@Example.com", kind="email", tenant_salt="s1")
    b = tokenize("  alice@example.com\n", kind="email", tenant_salt="s1")
    assert a == b


def test_tokenize_rejects_empty_value():
    with pytest.raises(ValueError):
        tokenize("   ", kind="email", tenant_salt="s1")


def test_tokenize_rejects_unknown_kind():
    with pytest.raises(ValueError):
        tokenize("x", kind="ssn", tenant_salt="s1")


def test_tokenize_known_kinds_complete():
    assert KNOWN_KINDS == frozenset({"email", "name", "phone", "address"})


def test_tokenize_missing_salt_raises(monkeypatch):
    monkeypatch.delenv("PII_TENANT_SALT", raising=False)

    # Stub the resolver so it returns nothing rather than reading
    # ambient env / secrets backends.
    from coherence_engine.server.fund.services import secret_manager

    class _NullResolver:
        def get(self, name):
            return None

    secret_manager.set_secret_resolver_for_tests(_NullResolver())
    try:
        with pytest.raises(PIITokenizationError):
            tokenize("x@example.com", kind="email")
    finally:
        secret_manager.set_secret_resolver_for_tests(None)


def test_is_token_false_for_clear_string():
    assert not is_token("alice@example.com")
    assert not is_token("")


# ---------------------------------------------------------------------------
# Founder.read_clear_email -- scope gate + audit row
# ---------------------------------------------------------------------------


def _seed_founder_with_clear_email(
    db, *, founder_id: str = "f_pii_1", email: str = "alice@example.com"
):
    """Insert a founder row with the new tokenization columns populated."""
    token = tokenize(email, kind="email")
    key_id, ciphertext = per_row_encryption.encrypt(
        email.encode("utf-8"),
        db=db,
        row_id=founder_id,
    )
    founder = models.Founder(
        id=founder_id,
        full_name="Alice",
        email=email,  # legacy column kept during expand phase
        company_name="ACo",
        country="US",
        email_token=token,
        email_clear=ciphertext,
        email_clear_key_id=key_id,
    )
    db.add(founder)
    db.flush()
    return founder, token


def test_read_clear_email_without_scope_raises():
    db = SessionLocal()
    try:
        founder, _ = _seed_founder_with_clear_email(db)
        principal = ClearReadPrincipal(
            id="key_xyz",
            kind="api_key",
            scopes=("applications:read",),
        )
        with pytest.raises(ClearReadDenied):
            founder.read_clear_email(
                db=db,
                principal=principal,
                route="/test",
                request_id="req_1",
            )
        # No audit row written for a denied read.
        assert db.query(PIIClearAuditLog).count() == 0
    finally:
        db.close()


def test_read_clear_email_with_scope_returns_clear_and_writes_audit():
    db = SessionLocal()
    try:
        founder, token = _seed_founder_with_clear_email(db)
        principal = ClearReadPrincipal(
            id="key_pii_reader",
            kind="api_key",
            scopes=(PII_READ_CLEAR_SCOPE,),
        )
        result = founder.read_clear_email(
            db=db,
            principal=principal,
            route="/api/v1/founders/{id}",
            request_id="req_xyz",
            reason="ops_lookup",
        )
        assert result == "alice@example.com"

        rows = db.query(PIIClearAuditLog).all()
        assert len(rows) == 1
        audit = rows[0]
        assert audit.principal_id == "key_pii_reader"
        assert audit.principal_kind == "api_key"
        assert audit.field_kind == "email"
        assert audit.token == token
        assert audit.subject_table == "fund_founders"
        assert audit.subject_id == "f_pii_1"
        assert audit.route == "/api/v1/founders/{id}"
        assert audit.request_id == "req_xyz"
        assert audit.reason == "ops_lookup"
        # The audit row must NOT contain the clear value anywhere.
        for col_value in (
            audit.token,
            audit.note,
            audit.reason,
            audit.route,
        ):
            assert "alice@example.com" not in col_value
    finally:
        db.close()


def test_two_clear_reads_produce_two_audit_rows():
    db = SessionLocal()
    try:
        founder, _ = _seed_founder_with_clear_email(db)
        principal = ClearReadPrincipal(
            id="key_pii_reader",
            kind="api_key",
            scopes=(PII_READ_CLEAR_SCOPE,),
        )
        founder.read_clear_email(db=db, principal=principal, request_id="r1")
        founder.read_clear_email(db=db, principal=principal, request_id="r2")
        assert db.query(PIIClearAuditLog).count() == 2
    finally:
        db.close()


def test_read_clear_email_decrypts_through_per_row_key():
    """End-to-end: token + cipher round-trip uses the AES-GCM helper."""
    db = SessionLocal()
    try:
        founder, _ = _seed_founder_with_clear_email(
            db, email="bob@example.org", founder_id="f_pii_2"
        )
        principal = ClearReadPrincipal(
            id="k", scopes=(PII_READ_CLEAR_SCOPE,)
        )
        assert (
            founder.read_clear_email(db=db, principal=principal)
            == "bob@example.org"
        )
    finally:
        db.close()
