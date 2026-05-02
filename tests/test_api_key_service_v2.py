"""Service-level tests for the v2 API-key model (prompt 28).

Covers Argon2id round-tripping, scope enforcement at the service level,
explicit-error reasons for expired / revoked keys, and rotation
behavior.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("argon2")

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services.api_key_service import (
    ApiKeyService,
    InvalidKey,
    KNOWN_SCOPES,
    _split_token,
)


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _make_account(db, name="scoring-worker"):
    sa = models.ServiceAccount(id=f"sa_{name}", name=name)
    db.add(sa)
    db.flush()
    return sa


def test_create_key_v2_stores_argon2id_hash_only():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["worker:claim", "worker:complete"],
            created_by="test",
        )
        db.commit()
        rec = (
            db.query(models.ApiKey)
            .filter(models.ApiKey.id == created.id)
            .one()
        )
        assert rec.key_hash.startswith("$argon2id$"), rec.key_hash
        # Plaintext token must never appear in the persisted hash.
        assert created.token not in rec.key_hash
        assert rec.prefix == created.prefix
        assert len(rec.prefix) == 8
        assert created.token.startswith(f"ce_{rec.prefix}_")
        assert json.loads(rec.scopes_json) == [
            "worker:claim",
            "worker:complete",
        ]
        assert rec.service_account_id == sa.id
    finally:
        db.close()


def test_verify_key_happy_path_returns_row():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["applications:read"],
            created_by="test",
        )
        db.commit()
        rec = svc.verify_key(db, created.token)
        assert rec.id == created.id
        assert rec.prefix == created.prefix
    finally:
        db.close()


def test_verify_key_tampered_secret_raises_invalid_key():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["applications:read"],
            created_by="test",
        )
        db.commit()
        bad = created.token[:-1] + ("Y" if created.token[-1] != "Y" else "Z")
        with pytest.raises(InvalidKey) as exc:
            svc.verify_key(db, bad)
        assert getattr(exc.value, "reason", None) == "unknown_key"
    finally:
        db.close()


def test_verify_key_unknown_prefix_raises_invalid_key():
    db = SessionLocal()
    try:
        svc = ApiKeyService()
        with pytest.raises(InvalidKey) as exc:
            svc.verify_key(db, "ce_zzzzzzzz_definitely-not-real")
        assert getattr(exc.value, "reason", None) == "unknown_key"
    finally:
        db.close()


def test_verify_key_malformed_token_raises_invalid_key():
    db = SessionLocal()
    try:
        svc = ApiKeyService()
        for bad in ("garbage", "cfk_oldstyle", "ce_short_x", ""):
            with pytest.raises(InvalidKey):
                svc.verify_key(db, bad)
    finally:
        db.close()


def test_verify_key_expired_returns_distinct_reason():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        past = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["worker:claim"],
            created_by="test",
            expires_at=past,
        )
        db.commit()
        with pytest.raises(InvalidKey) as exc:
            svc.verify_key(db, created.token)
        assert getattr(exc.value, "reason", None) == "expired"
    finally:
        db.close()


def test_verify_key_revoked_returns_distinct_reason():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["worker:claim"],
            created_by="test",
        )
        svc.revoke_key_v2(db, created.prefix)
        db.commit()
        with pytest.raises(InvalidKey) as exc:
            svc.verify_key(db, created.token)
        assert getattr(exc.value, "reason", None) == "revoked"
    finally:
        db.close()


def test_create_key_v2_rejects_unknown_scope():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        with pytest.raises(ValueError):
            svc.create_key_v2(
                db,
                service_account_id=sa.id,
                scopes=["bogus:scope"],
                created_by="test",
            )
    finally:
        db.close()


def test_create_key_v2_default_expiry_is_one_year():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        before = datetime.now(tz=timezone.utc)
        created = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["applications:read"],
            created_by="test",
        )
        db.commit()
        assert created.expires_at is not None
        delta = created.expires_at - before
        # ~365 days, allow ±1 day for execution skew.
        assert timedelta(days=364) <= delta <= timedelta(days=366)
    finally:
        db.close()


def test_rotate_key_v2_immediate_revoke():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        old = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["worker:claim", "worker:complete"],
            created_by="test",
        )
        db.commit()
        new_key = svc.rotate_key_v2(db, prefix=old.prefix, actor="test", grace_seconds=0)
        db.commit()
        assert new_key is not None
        assert new_key.prefix != old.prefix
        assert sorted(new_key.scopes) == sorted(["worker:claim", "worker:complete"])
        # Old token immediately rejected.
        with pytest.raises(InvalidKey) as exc:
            svc.verify_key(db, old.token)
        assert getattr(exc.value, "reason", None) == "revoked"
        # New token works.
        rec = svc.verify_key(db, new_key.token)
        assert rec.prefix == new_key.prefix
    finally:
        db.close()


def test_rotate_key_v2_grace_period_keeps_old_valid_briefly():
    db = SessionLocal()
    try:
        sa = _make_account(db)
        svc = ApiKeyService()
        old = svc.create_key_v2(
            db,
            service_account_id=sa.id,
            scopes=["worker:claim"],
            created_by="test",
        )
        db.commit()
        new_key = svc.rotate_key_v2(
            db, prefix=old.prefix, actor="test", grace_seconds=300
        )
        db.commit()
        assert new_key is not None
        # Within the grace window the old token still authenticates.
        rec = svc.verify_key(db, old.token)
        assert rec.prefix == old.prefix
        # But its expiry was shrunk to the grace cutoff. SQLite drops the
        # tzinfo, so coerce both sides to UTC-aware before comparing.
        expires_at = rec.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        assert expires_at <= datetime.now(tz=timezone.utc) + timedelta(seconds=305)
    finally:
        db.close()


def test_legacy_create_key_round_trips_via_argon2id():
    """The legacy ``create_key(repo, label, role, ...)`` shim must keep working."""
    from coherence_engine.server.fund.repositories.api_key_repository import (
        ApiKeyRepository,
    )

    db = SessionLocal()
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        info = svc.create_key(
            repo, label="legacy", role="admin", created_by="test", expires_in_days=30
        )
        db.commit()
        assert info["token"].startswith("ce_")
        rec = (
            db.query(models.ApiKey)
            .filter(models.ApiKey.id == info["id"])
            .one()
        )
        assert rec.key_hash.startswith("$argon2id$")
        result = svc.verify_token(repo, info["token"])
        assert result["ok"] is True
        assert result["role"] == "admin"
        assert "admin:write" in result["scopes"]
    finally:
        db.close()


def test_known_scopes_match_specification():
    assert KNOWN_SCOPES == frozenset(
        {
            "applications:read",
            "applications:write",
            "decisions:read",
            "admin:read",
            "admin:write",
            "worker:claim",
            "worker:complete",
        }
    )


def test_split_token_extracts_prefix():
    assert _split_token("ce_abcd1234_secretpart") == "abcd1234"
    assert _split_token("cfk_legacy") is None
    assert _split_token("ce_TOOSHORT_x") is None
    assert _split_token(None) is None  # type: ignore[arg-type]
