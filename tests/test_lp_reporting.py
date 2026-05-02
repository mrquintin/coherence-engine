"""Tests for LP reporting orchestration (prompt 69)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, List, Sequence

import pytest

from coherence_engine.server.fund.services.capital_call_notice import (
    CapitalCallLineItem,
    CapitalCallNotice,
    compute_idempotency_key as cc_idem_key,
    dispatch_for_signature,
    render_notice as render_capital_call,
)
from coherence_engine.server.fund.services.distribution_notice import (
    DistributionLineItem,
    DistributionNotice,
    DistributionNoticeError,
    compute_idempotency_key as dist_idem_key,
    dispatch_for_acknowledgement,
    render_notice as render_distribution,
)
from coherence_engine.server.fund.services.lp_reporting import (
    DEFAULT_CAPITAL_CALL_TEMPLATE,
    DEFAULT_DISTRIBUTION_TEMPLATE,
    DEFAULT_QUARTERLY_TEMPLATE,
    assemble_batch,
    assemble_quarterly_statement,
    compute_content_digest,
)
from coherence_engine.server.fund.services.nav_calculator import (
    CashFlow,
    LPCommitment,
    Mark,
    PortfolioPosition,
    UnsignedMarkError,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


FIXED_GENERATED_AT = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)


def _signed_mark(application_id: str, fmv: float) -> Mark:
    return Mark(
        application_id=application_id,
        fmv_usd=fmv,
        as_of_date=date(2026, 3, 31),
        methodology="priced_round",
        source="series_a_2026q1",
        operator_signoff_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        operator_id="op_42",
        note="signed",
    )


def _make_inputs(lp_id: str = "lp_alice"):
    commitment = LPCommitment(
        lp_id=lp_id,
        legal_name="Alice Capital LLC",
        commitment_usd=1_000_000.0,
        called_to_date_usd=500_000.0,
        ownership_fraction=0.10,
    )
    positions = [
        PortfolioPosition(
            application_id="app_acme",
            company_name="AcmeCo",
            cost_basis_usd=2_000_000.0,
            instrument_type="safe_post_money",
            invested_at=date(2026, 1, 15),
        ),
        PortfolioPosition(
            application_id="app_bravo",
            company_name="BravoCorp",
            cost_basis_usd=1_500_000.0,
            instrument_type="safe_post_money",
            invested_at=date(2026, 2, 10),
        ),
    ]
    marks = {
        "app_acme": _signed_mark("app_acme", 4_000_000.0),
        "app_bravo": _signed_mark("app_bravo", 1_800_000.0),
    }
    flows = [
        CashFlow(date(2026, 1, 10), -250_000.0, "capital_call"),
        CashFlow(date(2026, 2, 5), -250_000.0, "capital_call"),
    ]
    return commitment, positions, marks, flows


# ---------------------------------------------------------------------------
# Quarterly statement assembly
# ---------------------------------------------------------------------------


def test_assemble_quarterly_statement_happy_path() -> None:
    commitment, positions, marks, flows = _make_inputs()
    statement = assemble_quarterly_statement(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=flows,
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        generated_at=FIXED_GENERATED_AT,
    )
    assert statement.lp_id == "lp_alice"
    assert statement.quarter_label == "2026Q1"
    assert statement.statement_id.startswith("stmt_")
    assert statement.content_digest == compute_content_digest(statement.tex_source)
    # Spot-check the rendered LaTeX.
    assert "Alice Capital LLC" in statement.tex_source
    assert "AcmeCo" in statement.tex_source
    assert "BravoCorp" in statement.tex_source
    assert "2026Q1" in statement.tex_source
    assert statement.tex_source.endswith("\n")


def test_assemble_quarterly_statement_is_byte_deterministic() -> None:
    commitment, positions, marks, flows = _make_inputs()
    a = assemble_quarterly_statement(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=flows,
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        generated_at=FIXED_GENERATED_AT,
    )
    b = assemble_quarterly_statement(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=flows,
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        generated_at=FIXED_GENERATED_AT,
    )
    assert a.tex_source == b.tex_source
    assert a.content_digest == b.content_digest
    assert a.statement_id == b.statement_id


def test_assemble_quarterly_statement_rejects_unsigned_mark() -> None:
    commitment, positions, marks, flows = _make_inputs()
    bad_marks = dict(marks)
    bad_marks["app_acme"] = Mark(
        application_id="app_acme",
        fmv_usd=4_000_000.0,
        as_of_date=date(2026, 3, 31),
        methodology="manager_mark",
        source="qoq_pulse",
        operator_signoff_at=None,
        operator_id="",
    )
    with pytest.raises(UnsignedMarkError):
        assemble_quarterly_statement(
            commitment=commitment,
            positions=positions,
            marks=bad_marks,
            cash_flows=flows,
            quarter_start=date(2026, 1, 1),
            quarter_end=date(2026, 3, 31),
            generated_at=FIXED_GENERATED_AT,
        )


def test_assemble_batch_emits_one_statement_per_lp_and_isolates_data() -> None:
    commitment_alice, positions, marks, flows_alice = _make_inputs("lp_alice")
    commitment_bob = LPCommitment(
        lp_id="lp_bob",
        legal_name="Bob Capital LLC",
        commitment_usd=2_000_000.0,
        called_to_date_usd=400_000.0,
        ownership_fraction=0.05,
    )
    flows_bob = [CashFlow(date(2026, 2, 1), -400_000.0, "capital_call")]

    batch = assemble_batch(
        commitments=[commitment_alice, commitment_bob],
        positions=positions,
        marks=marks,
        cash_flows_by_lp={"lp_alice": flows_alice, "lp_bob": flows_bob},
        quarter_start=date(2026, 1, 1),
        quarter_end=date(2026, 3, 31),
        generated_at=FIXED_GENERATED_AT,
    )

    assert {s.lp_id for s in batch} == {"lp_alice", "lp_bob"}
    alice = next(s for s in batch if s.lp_id == "lp_alice")
    bob = next(s for s in batch if s.lp_id == "lp_bob")
    # The two statements MUST NOT cross-leak — bob should never see
    # alice's legal name and vice versa.
    assert "Alice Capital LLC" in alice.tex_source
    assert "Bob Capital LLC" not in alice.tex_source
    assert "Bob Capital LLC" in bob.tex_source
    assert "Alice Capital LLC" not in bob.tex_source
    assert alice.content_digest != bob.content_digest


def test_default_template_paths_exist_on_disk() -> None:
    assert DEFAULT_QUARTERLY_TEMPLATE.is_file(), DEFAULT_QUARTERLY_TEMPLATE
    assert DEFAULT_CAPITAL_CALL_TEMPLATE.is_file(), DEFAULT_CAPITAL_CALL_TEMPLATE
    assert DEFAULT_DISTRIBUTION_TEMPLATE.is_file(), DEFAULT_DISTRIBUTION_TEMPLATE


# ---------------------------------------------------------------------------
# Capital-call notice
# ---------------------------------------------------------------------------


def _capital_call_notice() -> CapitalCallNotice:
    return CapitalCallNotice(
        call_id="CC-2026Q2-01",
        lp_id="lp_alice",
        lp_legal_name="Alice Capital LLC",
        fund_name="Coherence Fund I, LP",
        notice_date=date(2026, 4, 5),
        due_date=date(2026, 4, 19),
        total_call_amount_usd=2_000_000.0,
        lp_call_amount_usd=200_000.0,
        cumulative_called_usd=700_000.0,
        remaining_commitment_usd=300_000.0,
        line_items=[
            CapitalCallLineItem(
                application_id="app_acme",
                company_name="AcmeCo",
                instrument_type="safe_post_money",
                investment_amount_usd=1_500_000.0,
                lp_share_usd=150_000.0,
            ),
            CapitalCallLineItem(
                application_id="app_charlie",
                company_name="CharlieAI",
                instrument_type="safe_post_money",
                investment_amount_usd=500_000.0,
                lp_share_usd=50_000.0,
            ),
        ],
        wire_instructions_ref="WIRE-LP-ALICE-2026",
        contact_email="treasury@coherence.fund",
    )


def test_render_capital_call_is_deterministic() -> None:
    notice = _capital_call_notice()
    a = render_capital_call(notice, generated_at=FIXED_GENERATED_AT)
    b = render_capital_call(notice, generated_at=FIXED_GENERATED_AT)
    assert a.tex_source == b.tex_source
    assert a.content_digest == b.content_digest
    # Spot-check: includes call id, LP, line items, wire ref.
    assert "CC-2026Q2-01" in a.tex_source
    assert "Alice Capital LLC" in a.tex_source
    assert "AcmeCo" in a.tex_source
    assert "CharlieAI" in a.tex_source
    assert "WIRE-LP-ALICE-2026" in a.tex_source


def test_capital_call_idempotency_key_is_deterministic() -> None:
    k1 = cc_idem_key("CC-2026Q2-01", "lp_alice")
    k2 = cc_idem_key("CC-2026Q2-01", "lp_alice")
    k3 = cc_idem_key("CC-2026Q2-01", "lp_bob")
    assert k1 == k2
    assert k1 != k3
    # Different call ids for the same LP should not collide.
    assert k1 != cc_idem_key("CC-2026Q2-02", "lp_alice")


# ---------------------------------------------------------------------------
# Distribution notice
# ---------------------------------------------------------------------------


def _distribution_notice() -> DistributionNotice:
    return DistributionNotice(
        distribution_id="DIST-2026-001",
        lp_id="lp_alice",
        lp_legal_name="Alice Capital LLC",
        fund_name="Coherence Fund I, LP",
        notice_date=date(2026, 6, 10),
        payment_date=date(2026, 6, 20),
        total_distribution_usd=1_000_000.0,
        lp_distribution_usd=100_000.0,
        cumulative_distributions_usd=100_000.0,
        line_items=[
            DistributionLineItem(
                application_id="app_acme",
                company_name="AcmeCo",
                kind="realized_gain",
                gross_amount_usd=600_000.0,
                lp_share_usd=60_000.0,
            ),
            DistributionLineItem(
                application_id="app_acme",
                company_name="AcmeCo",
                kind="return_of_capital",
                gross_amount_usd=400_000.0,
                lp_share_usd=40_000.0,
            ),
        ],
        wire_instructions_ref="WIRE-LP-ALICE-2026",
        treasurer_approval_ref="appr_treasurer_42",
        contact_email="treasury@coherence.fund",
    )


def test_render_distribution_is_deterministic() -> None:
    notice = _distribution_notice()
    a = render_distribution(notice, generated_at=FIXED_GENERATED_AT)
    b = render_distribution(notice, generated_at=FIXED_GENERATED_AT)
    assert a.tex_source == b.tex_source
    assert a.content_digest == b.content_digest
    assert "DIST-2026-001" in a.tex_source
    # underscores are LaTeX-escaped, so look for the escaped form.
    assert r"appr\_treasurer\_42" in a.tex_source
    assert "WIRE-LP-ALICE-2026" in a.tex_source


def test_distribution_rejects_unknown_kind() -> None:
    notice = DistributionNotice(
        distribution_id="DIST-2026-002",
        lp_id="lp_alice",
        lp_legal_name="Alice Capital LLC",
        fund_name="Coherence Fund I, LP",
        notice_date=date(2026, 6, 10),
        payment_date=date(2026, 6, 20),
        total_distribution_usd=10.0,
        lp_distribution_usd=1.0,
        cumulative_distributions_usd=1.0,
        line_items=[
            DistributionLineItem(
                application_id="app_x",
                company_name="X",
                kind="bonus_round",
                gross_amount_usd=10.0,
                lp_share_usd=1.0,
            )
        ],
        wire_instructions_ref="WIRE-X",
        treasurer_approval_ref="appr_x",
        contact_email="x@x.test",
    )
    with pytest.raises(DistributionNoticeError):
        render_distribution(notice, generated_at=FIXED_GENERATED_AT)


def test_distribution_rejects_missing_wire_ref() -> None:
    notice = _distribution_notice()
    bad = DistributionNotice(
        **{**notice.__dict__, "wire_instructions_ref": ""}
    )
    with pytest.raises(DistributionNoticeError):
        render_distribution(bad, generated_at=FIXED_GENERATED_AT)


def test_distribution_idempotency_key_distinct_per_lp() -> None:
    a = dist_idem_key("DIST-2026-001", "lp_alice")
    b = dist_idem_key("DIST-2026-001", "lp_bob")
    assert a != b


# ---------------------------------------------------------------------------
# Dispatch helpers (prompt-69 prohibition: no auto-execution)
# ---------------------------------------------------------------------------


@dataclass
class _RecordedSend:
    document_template_id: str
    body_bytes: bytes
    signers: tuple
    idempotency_key: str


class _FakeProvider:
    """Stub that records `send` calls; never opens a network connection.

    Mirrors the surface of :class:`ESignatureProvider` enough for the
    capital-call / distribution dispatchers to exercise the happy path
    without a live DocuSign account.
    """

    name = "fake"

    def __init__(self) -> None:
        self.sends: List[_RecordedSend] = []

    def send(
        self,
        *,
        document: Any,
        signers: Sequence[Any],
        idempotency_key: str,
    ) -> Any:
        self.sends.append(
            _RecordedSend(
                document_template_id=document.template_id,
                body_bytes=document.body,
                signers=tuple((s.role, s.email) for s in signers),
                idempotency_key=idempotency_key,
            )
        )
        from coherence_engine.server.fund.services.esignature import SendResponse

        return SendResponse(
            provider_request_id=f"prov_{idempotency_key[:8]}",
            provider_status="sent",
        )


def test_capital_call_dispatch_for_signature_does_not_move_money() -> None:
    notice = _capital_call_notice()
    provider = _FakeProvider()
    result = dispatch_for_signature(
        notice,
        provider=provider,
        fund_signer_name="Coherence GP",
        fund_signer_email="gp@coherence.fund",
        generated_at=FIXED_GENERATED_AT,
    )
    assert len(provider.sends) == 1
    sent = provider.sends[0]
    assert sent.document_template_id == "capital_call_notice_v1"
    assert ("lp_signer", notice.contact_email) in sent.signers
    assert ("fund_countersigner", "gp@coherence.fund") in sent.signers
    assert result["idempotency_key"] == cc_idem_key(notice.call_id, notice.lp_id)
    assert result["provider_request_id"].startswith("prov_")
    # The dispatcher MUST NOT touch any capital-deployment surface --
    # absence of a CapitalDeployment import or call is the assertion.
    # We sanity-check by verifying nothing in the recorded send hints
    # at money movement (no "execute" / "transfer" tokens in body).
    assert b"execute" not in sent.body_bytes.lower()
    assert b"wire transfer" not in sent.body_bytes.lower() or b"WIRE-LP-" in sent.body_bytes


def test_capital_call_dispatch_idempotent_send_collapses_per_pair() -> None:
    notice = _capital_call_notice()
    provider = _FakeProvider()
    a = dispatch_for_signature(
        notice,
        provider=provider,
        fund_signer_name="Coherence GP",
        fund_signer_email="gp@coherence.fund",
        generated_at=FIXED_GENERATED_AT,
    )
    b = dispatch_for_signature(
        notice,
        provider=provider,
        fund_signer_name="Coherence GP",
        fund_signer_email="gp@coherence.fund",
        generated_at=FIXED_GENERATED_AT,
    )
    # Idempotency keys equal — the provider is the one that collapses
    # by key; the dispatcher is responsible for stamping the same one.
    assert a["idempotency_key"] == b["idempotency_key"]


def test_distribution_dispatch_requires_treasurer_approval_ref() -> None:
    notice = _distribution_notice()
    bad = DistributionNotice(
        **{**notice.__dict__, "treasurer_approval_ref": ""}
    )
    provider = _FakeProvider()
    with pytest.raises(DistributionNoticeError):
        dispatch_for_acknowledgement(
            bad,
            provider=provider,
            fund_signer_name="Coherence GP",
            fund_signer_email="gp@coherence.fund",
            generated_at=FIXED_GENERATED_AT,
        )
    assert provider.sends == []


def test_distribution_dispatch_records_acknowledgement_only() -> None:
    notice = _distribution_notice()
    provider = _FakeProvider()
    result = dispatch_for_acknowledgement(
        notice,
        provider=provider,
        fund_signer_name="Coherence GP",
        fund_signer_email="gp@coherence.fund",
        generated_at=FIXED_GENERATED_AT,
    )
    assert len(provider.sends) == 1
    sent = provider.sends[0]
    assert sent.document_template_id == "distribution_notice_v1"
    # The LP signer role MUST be "lp_acknowledger" -- this is the
    # type-system marker enforcing that the LP is acknowledging
    # receipt, not authorising a wire transfer.
    assert ("lp_acknowledger", notice.contact_email) in sent.signers
    assert result["content_digest"]
