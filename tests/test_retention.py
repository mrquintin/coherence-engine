"""Retention sweep + crypto-shred tests (prompt 57).

Covers:

* policy YAML loads and rejects malformed entries
* a transcript past its 90-day horizon gets tombstoned + key-shredded +
  flagged ``redacted=True``
* decision-artifact and audit-log rows are inspected but never modified
* tombstone+shred is idempotent across re-runs
* a decryption attempt against a shredded key raises
  :class:`KeyShreddedError` (the post-shred read contract)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services.crypto_shred import (
    is_shredded,
    shred_key,
)
from coherence_engine.server.fund.services.per_row_encryption import (
    KeyShreddedError,
    decrypt,
    encrypt,
    set_encryption_key_store,
)
from coherence_engine.server.fund.services.retention import (
    ON_EXPIRY_KEEP,
    ON_EXPIRY_TOMBSTONE_AND_SHRED,
    SCHEMA_VERSION,
    apply_retention,
    load_retention_policy,
)


@pytest.fixture(autouse=True)
def _reset_fund_tables(tmp_path):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    set_encryption_key_store(None)
    # Use a local-filesystem object-storage backend so the tombstone
    # path runs end-to-end. The retention sweep tolerates missing
    # blobs but exercising the real backend catches accidental
    # contract drift.
    import os

    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["LOCAL_STORAGE_ROOT"] = str(tmp_path / "obj")
    object_storage.reset_object_storage()
    yield
    object_storage.reset_object_storage()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _seed_founder_app(
    db,
    *,
    founder_id: str = "fnd_ret_01",
    application_id: str = "app_ret_01",
    transcript_text: bytes = b"founder said: we help compliance teams",
    age_days: int = 120,
) -> tuple[models.Application, str, str]:
    """Insert a founder + application with an encrypted transcript blob.

    Returns ``(application, key_id, ciphertext_b64)``. The
    application's ``created_at`` is back-dated by ``age_days`` so the
    sweep treats it as past the 90-day transcript retention.
    """
    founder = models.Founder(
        id=founder_id,
        full_name="Test Founder",
        email="founder@example.com",
        company_name="Compliance Co",
        country="US",
    )
    db.add(founder)
    db.flush()
    # Put the transcript blob in object storage so retention has a URI
    # to tombstone.
    put_result = object_storage.put(
        f"transcripts/{application_id}.txt",
        transcript_text,
        content_type="text/plain",
    )
    key_id, ct = encrypt(
        transcript_text,
        db=db,
        row_id=application_id,
    )
    backdated = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    app = models.Application(
        id=application_id,
        founder_id=founder.id,
        one_liner="x",
        requested_check_usd=100000,
        use_of_funds_summary="x",
        preferred_channel="email",
        transcript_text=ct,
        transcript_uri=put_result.uri,
        transcript_key_id=key_id,
        created_at=backdated,
    )
    db.add(app)
    db.flush()
    return app, key_id, ct


def test_policy_loads_and_declares_keep_for_decision_artifact():
    pol = load_retention_policy()
    assert pol.schema_version == SCHEMA_VERSION
    names = {c.name for c in pol.classes}
    assert {"transcript", "decision_artifact", "kyc_evidence", "audit_log"}.issubset(names)
    decision = pol.by_name("decision_artifact")
    assert decision is not None
    assert decision.on_expiry == ON_EXPIRY_KEEP
    assert decision.retention_days is None
    assert pol.is_audit_hold("decision_artifact") is True
    assert pol.is_audit_hold("transcript") is False
    transcript = pol.by_name("transcript")
    assert transcript.retention_days == 90
    assert transcript.on_expiry == ON_EXPIRY_TOMBSTONE_AND_SHRED


def test_policy_rejects_keep_with_finite_retention(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: retention-policy-v1\n"
        "classes:\n"
        "  - name: bogus\n"
        "    retention_days: 30\n"
        "    on_expiry: keep\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="indefinite"):
        load_retention_policy(bad)


def test_apply_retention_tombstones_and_shreds_old_transcript():
    db = SessionLocal()
    try:
        app, key_id, _ = _seed_founder_app(db)
        db.commit()
        # Sanity: decryption works pre-shred.
        plain = decrypt(app.transcript_text, db=db, row_id=app.id, key_id=key_id)
        assert plain == b"founder said: we help compliance teams"
        result = apply_retention(db)
        db.commit()
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        refreshed = db2.get(models.Application, "app_ret_01")
        assert refreshed.redacted is True
        assert refreshed.redacted_at is not None
        assert refreshed.redaction_reason == "retention:transcript"
        assert is_shredded(db2, key_id) is True
        with pytest.raises(KeyShreddedError):
            decrypt(refreshed.transcript_text, db=db2, row_id=refreshed.id, key_id=key_id)
        # Sweep stats should reflect at least one redaction on transcript.
        transcript_stats = next(s for s in result.stats if s.class_name == "transcript")
        assert transcript_stats.tombstoned >= 1
        assert transcript_stats.shredded == 1
    finally:
        db2.close()


def test_apply_retention_skips_kept_classes_and_leaves_decision_rows_alone():
    """decision_artifact is on_expiry=keep -- the sweep MUST NOT touch it."""
    db = SessionLocal()
    try:
        # Seed a Decision row directly (no key_id, no redacted column).
        founder = models.Founder(
            id="fnd_keep_01",
            full_name="K",
            email="k@example.com",
            company_name="Kco",
            country="US",
        )
        db.add(founder)
        app = models.Application(
            id="app_keep_01",
            founder_id=founder.id,
            one_liner="x",
            requested_check_usd=100000,
            use_of_funds_summary="x",
            preferred_channel="email",
            created_at=datetime.now(tz=timezone.utc) - timedelta(days=400),
        )
        db.add(app)
        decision = models.Decision(
            id="dec_keep_01",
            application_id=app.id,
            decision="pass",
            policy_version="v1",
            parameter_set_id="ps",
            threshold_required=0.1,
            coherence_observed=0.5,
            margin=0.4,
        )
        db.add(decision)
        db.commit()
        result = apply_retention(db)
        db.commit()
        decision_stats = next(s for s in result.stats if s.class_name == "decision_artifact")
        assert decision_stats.tombstoned == 0
        assert decision_stats.shredded == 0
        # Decision row must still exist and be untouched.
        refreshed = db.get(models.Decision, "dec_keep_01")
        assert refreshed is not None
        assert refreshed.decision == "pass"
    finally:
        db.close()


def test_tombstone_and_shred_is_idempotent():
    """Re-running the sweep over an already-redacted row is a no-op."""
    db = SessionLocal()
    try:
        app, key_id, _ = _seed_founder_app(db, application_id="app_idem_01", founder_id="fnd_idem_01")
        db.commit()
        first = apply_retention(db)
        db.commit()
        second = apply_retention(db)
        db.commit()
        first_t = next(s for s in first.stats if s.class_name == "transcript")
        second_t = next(s for s in second.stats if s.class_name == "transcript")
        assert first_t.shredded == 1
        assert first_t.tombstoned >= 1
        # Second run must not re-shred / re-tombstone. Already-redacted rows
        # are filtered out by the sweep query, so ``inspected`` falls to 0
        # rather than being counted as ``skipped_already_redacted``.
        assert second_t.inspected == 0
        assert second_t.shredded == 0
        assert second_t.tombstoned == 0
        # Key shredding itself is also idempotent.
        assert shred_key(db, key_id) is False
    finally:
        db.close()


def test_shred_key_raises_on_unknown_id():
    from coherence_engine.server.fund.services.per_row_encryption import (
        KeyNotFoundError,
    )

    db = SessionLocal()
    try:
        with pytest.raises(KeyNotFoundError):
            shred_key(db, "key_does_not_exist")
    finally:
        db.close()


def test_aes_gcm_aad_binding_rejects_swapped_ciphertext():
    """Ciphertext from row A pasted into row B must fail authentication."""
    from coherence_engine.server.fund.services.per_row_encryption import (
        CiphertextCorrupt,
    )

    db = SessionLocal()
    try:
        key_id_a, ct_a = encrypt(b"row a payload", db=db, row_id="row_a")
        db.commit()
        # Decrypting under row_b must fail (AAD mismatch).
        with pytest.raises(CiphertextCorrupt):
            decrypt(ct_a, db=db, row_id="row_b", key_id=key_id_a)
        # And the legitimate decryption still works.
        assert decrypt(ct_a, db=db, row_id="row_a", key_id=key_id_a) == b"row a payload"
    finally:
        db.close()
