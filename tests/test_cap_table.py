"""Cap-table integration tests (prompt 68).

Covers:

* Backend record + fetch round-trip on the in-tree synthetic Carta
  and Pulley paths (no real HTTP).
* Service preconditions: ``record_issuance`` refuses to write a row
  unless the application has BOTH a signed SAFE and a sent
  investment instruction (load-bearing prompt-68 safety check).
* Service idempotency: repeated ``record_issuance`` with the same
  idempotency key collapses onto a single row and does NOT re-call
  the backend.
* Reconciliation: matching local + provider records advance the row
  to ``reconciled``; a forged divergence is surfaced as a
  :class:`ReconciliationFinding` and the local row is NOT mutated.
* ``ApplicationService.maybe_sync_cap_table`` returns ``None`` when
  preconditions are not satisfied and a descriptor when they are.
"""

from __future__ import annotations

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services.cap_table import (
    ALLOWED_INSTRUMENT_TYPES,
    CapTableError,
    CapTableService,
    PreconditionsNotMet,
    ReconciliationFinding,
    compute_idempotency_key,
    preconditions_satisfied,
)
from coherence_engine.server.fund.services.cap_table_backends import (
    CartaBackend,
    PulleyBackend,
    backend_for_provider,
)


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
def carta_backend():
    return CartaBackend(api_token="carta-test-token")


@pytest.fixture
def pulley_backend():
    return PulleyBackend(api_token="pulley-test-token")


def _persist_application(
    app_id: str = "app_captable_1",
) -> tuple[models.Founder, models.Application]:
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_captable_1",
            full_name="Cap Founder",
            email="cap@example.com",
            country="US",
            company_name="Cap Co",
        )
        application = models.Application(
            id=app_id,
            founder_id=founder.id,
            one_liner="Cap-table pilot",
            requested_check_usd=100_000,
            use_of_funds_summary="Seed",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="decision_issued",
            scoring_mode="enforce",
        )
        db.add_all([founder, application])
        db.commit()
        db.refresh(founder)
        db.refresh(application)
        return founder, application
    finally:
        db.close()


def _persist_signed_safe(application_id: str) -> models.SignatureRequest:
    db = SessionLocal()
    try:
        row = models.SignatureRequest(
            id="sig_captable_1",
            application_id=application_id,
            document_template="safe_note_v1",
            template_vars_hash="abc",
            provider="docusign",
            provider_request_id="dsx_1",
            status="signed",
            signed_pdf_uri="coh://sigs/captable/1.pdf",
            signers_json="[]",
            idempotency_key="ik_sig_1",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _persist_sent_instruction(
    application_id: str, founder_id: str, amount_usd: int = 100_000
) -> models.InvestmentInstruction:
    db = SessionLocal()
    try:
        row = models.InvestmentInstruction(
            id="ins_captable_1",
            application_id=application_id,
            founder_id=founder_id,
            amount_usd=amount_usd,
            currency="USD",
            target_account_ref="cp_x",
            preparation_method="bank_transfer",
            status="sent",
            provider_intent_ref="pmt_1",
            idempotency_key="ik_ins_1",
            prepared_by="partner:alice",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backend unit tests
# ---------------------------------------------------------------------------


def test_carta_record_then_fetch_roundtrip(carta_backend):
    class _Stub:
        idempotency_key = "ik_x"
        instrument_type = "safe_post_money"
        amount_usd = 50_000
        valuation_cap_usd = 5_000_000
        discount = 0.20

    response = carta_backend.record_issuance(issuance=_Stub())
    assert response.provider_issuance_id.startswith("carta_iss_")
    record = carta_backend.fetch_issuance(
        provider_issuance_id=response.provider_issuance_id
    )
    assert record.amount_usd == 50_000
    assert record.instrument_type == "safe_post_money"


def test_pulley_record_then_fetch_roundtrip(pulley_backend):
    class _Stub:
        idempotency_key = "ik_y"
        instrument_type = "priced_round_preferred"
        amount_usd = 250_000
        valuation_cap_usd = 0
        discount = 0.0

    response = pulley_backend.record_issuance(issuance=_Stub())
    assert response.provider_issuance_id.startswith("pulley_iss_")
    record = pulley_backend.fetch_issuance(
        provider_issuance_id=response.provider_issuance_id
    )
    assert record.instrument_type == "priced_round_preferred"


def test_carta_record_is_deterministic_per_idempotency_key(carta_backend):
    class _Stub:
        idempotency_key = "ik_stable"
        instrument_type = "safe_post_money"
        amount_usd = 10_000
        valuation_cap_usd = 0
        discount = 0.0

    first = carta_backend.record_issuance(issuance=_Stub())
    second = carta_backend.record_issuance(issuance=_Stub())
    assert first.provider_issuance_id == second.provider_issuance_id


def test_backend_for_provider_unknown_raises():
    with pytest.raises(ValueError):
        backend_for_provider("unknown")


# ---------------------------------------------------------------------------
# Service preconditions
# ---------------------------------------------------------------------------


def test_preconditions_not_met_without_signed_safe(carta_backend):
    _f, app = _persist_application()
    db = SessionLocal()
    try:
        service = CapTableService(db)
        with pytest.raises(PreconditionsNotMet):
            service.record_issuance(
                backend=carta_backend,
                application_id=app.id,
                instrument_type="safe_post_money",
                amount_usd=100_000,
            )
    finally:
        db.close()


def test_preconditions_not_met_without_sent_instruction(carta_backend):
    _f, app = _persist_application()
    _persist_signed_safe(app.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        with pytest.raises(PreconditionsNotMet):
            service.record_issuance(
                backend=carta_backend,
                application_id=app.id,
                instrument_type="safe_post_money",
                amount_usd=100_000,
            )
    finally:
        db.close()


def test_preconditions_satisfied_helper_returns_pair():
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        sig, ins = preconditions_satisfied(db, application_id=app.id)
        assert sig is not None and ins is not None
        assert sig.status == "signed"
        assert ins.status == "sent"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: record_issuance
# ---------------------------------------------------------------------------


def test_record_issuance_happy_path(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        row = service.record_issuance(
            backend=carta_backend,
            application_id=app.id,
            instrument_type="safe_post_money",
            amount_usd=100_000,
            valuation_cap_usd=5_000_000,
            discount=0.20,
        )
        db.commit()
        assert row.status == "recorded"
        assert row.provider == "carta"
        assert row.provider_issuance_id.startswith("carta_iss_")
        assert row.recorded_at is not None
    finally:
        db.close()


def test_record_issuance_is_idempotent(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    ins = _persist_sent_instruction(app.id, f.id)
    key = compute_idempotency_key(app.id, "safe_post_money", salt=ins.id)

    db = SessionLocal()
    try:
        service = CapTableService(db)
        first = service.record_issuance(
            backend=carta_backend,
            application_id=app.id,
            instrument_type="safe_post_money",
            amount_usd=100_000,
            idempotency_key=key,
        )
        db.commit()
        second = service.record_issuance(
            backend=carta_backend,
            application_id=app.id,
            instrument_type="safe_post_money",
            amount_usd=100_000,
            idempotency_key=key,
        )
        db.commit()
        assert first.id == second.id
        rows = (
            db.query(models.CapTableIssuance)
            .filter(models.CapTableIssuance.application_id == app.id)
            .all()
        )
        assert len(rows) == 1
    finally:
        db.close()


def test_record_issuance_rejects_invalid_instrument_type(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        with pytest.raises(CapTableError):
            service.record_issuance(
                backend=carta_backend,
                application_id=app.id,
                instrument_type="convertible_note",
                amount_usd=100_000,
            )
    finally:
        db.close()


def test_record_issuance_rejects_zero_amount(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        with pytest.raises(CapTableError):
            service.record_issuance(
                backend=carta_backend,
                application_id=app.id,
                instrument_type="safe_post_money",
                amount_usd=0,
            )
    finally:
        db.close()


def test_allowed_instrument_types_vocabulary():
    assert "safe_post_money" in ALLOWED_INSTRUMENT_TYPES
    assert "safe_pre_money" in ALLOWED_INSTRUMENT_TYPES
    assert "priced_round_preferred" in ALLOWED_INSTRUMENT_TYPES


# ---------------------------------------------------------------------------
# Service: reconcile
# ---------------------------------------------------------------------------


def test_reconcile_marks_matching_rows_reconciled(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        row = service.record_issuance(
            backend=carta_backend,
            application_id=app.id,
            instrument_type="safe_post_money",
            amount_usd=100_000,
            valuation_cap_usd=5_000_000,
            discount=0.20,
        )
        db.commit()
        report = service.reconcile(backend=carta_backend)
        db.commit()
        assert report.checked == 1
        assert report.reconciled == 1
        assert report.divergent == []
        assert report.ok is True
        db.refresh(row)
        assert row.status == "reconciled"
    finally:
        db.close()


def test_reconcile_flags_divergence_and_does_not_mutate_local(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        row = service.record_issuance(
            backend=carta_backend,
            application_id=app.id,
            instrument_type="safe_post_money",
            amount_usd=100_000,
            valuation_cap_usd=5_000_000,
            discount=0.20,
        )
        db.commit()

        # Forge a divergence on the provider side: rewrite the
        # synthetic ledger entry for this issuance with a different
        # amount. The reconciler must SURFACE the divergence and
        # MUST NOT silently rewrite the local row.
        from coherence_engine.server.fund.services.cap_table_backends import (
            ProviderRecord,
        )

        carta_backend._ledger.put(
            row.provider_issuance_id,
            ProviderRecord(
                provider_issuance_id=row.provider_issuance_id,
                instrument_type="safe_post_money",
                amount_usd=999_999,
                valuation_cap_usd=5_000_000,
                discount=0.20,
                status="recorded",
            ),
        )

        report = service.reconcile(backend=carta_backend)
        db.commit()
        assert report.checked == 1
        assert report.reconciled == 0
        assert len(report.divergent) == 1
        finding = report.divergent[0]
        assert isinstance(finding, ReconciliationFinding)
        assert finding.field == "amount_usd"
        assert finding.local_value == 100_000
        assert finding.provider_value == 999_999
        assert report.ok is False

        db.refresh(row)
        # Critical prompt-68 invariant: local row is unchanged.
        assert row.amount_usd == 100_000
        assert row.status == "recorded"
    finally:
        db.close()


def test_reconcile_reports_missing_remote(carta_backend):
    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        service = CapTableService(db)
        row = service.record_issuance(
            backend=carta_backend,
            application_id=app.id,
            instrument_type="safe_post_money",
            amount_usd=100_000,
        )
        db.commit()
        # Wipe the synthetic ledger so the provider returns nothing.
        carta_backend._ledger.reset()
        report = service.reconcile(backend=carta_backend)
        assert report.missing_remote == [row.id]
        assert report.ok is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ApplicationService.maybe_sync_cap_table
# ---------------------------------------------------------------------------


def test_application_service_returns_none_when_preconditions_unmet(
    carta_backend,
):
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import (
        EventPublisher,
    )

    _f, app = _persist_application()
    db = SessionLocal()
    try:
        repo = ApplicationRepository(db)
        events = EventPublisher(db)
        svc = ApplicationService(repo, events)
        result = svc.maybe_sync_cap_table(app.id, backend=carta_backend)
        assert result is None
    finally:
        db.close()


def test_application_service_records_when_preconditions_met(carta_backend):
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import (
        EventPublisher,
    )

    f, app = _persist_application()
    _persist_signed_safe(app.id)
    _persist_sent_instruction(app.id, f.id)
    db = SessionLocal()
    try:
        repo = ApplicationRepository(db)
        events = EventPublisher(db)
        svc = ApplicationService(repo, events)
        result = svc.maybe_sync_cap_table(
            app.id,
            backend=carta_backend,
            instrument_type="safe_post_money",
            valuation_cap_usd=5_000_000,
            discount=0.20,
        )
        db.commit()
        assert result is not None
        assert result["status"] == "recorded"
        assert result["provider"] == "carta"

        # Idempotent re-invocation collapses onto the same row.
        again = svc.maybe_sync_cap_table(
            app.id,
            backend=carta_backend,
            instrument_type="safe_post_money",
            valuation_cap_usd=5_000_000,
            discount=0.20,
        )
        db.commit()
        assert again["issuance_id"] == result["issuance_id"]
        rows = (
            db.query(models.CapTableIssuance)
            .filter(models.CapTableIssuance.application_id == app.id)
            .all()
        )
        assert len(rows) == 1
    finally:
        db.close()
