"""Tests for the per-LP NAV calculator (prompt 69)."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from coherence_engine.server.fund.services.nav_calculator import (
    CashFlow,
    InvalidInputError,
    LPCommitment,
    Mark,
    PortfolioPosition,
    UnsignedMarkError,
    compute_irr,
    compute_nav,
)


def _signed_mark(
    application_id: str,
    fmv: float,
    *,
    methodology: str = "priced_round",
    source: str = "priced_round_2026q1",
) -> Mark:
    return Mark(
        application_id=application_id,
        fmv_usd=fmv,
        as_of_date=date(2026, 3, 31),
        methodology=methodology,
        source=source,
        operator_signoff_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
        operator_id="op_42",
        note="signed",
    )


def _commit(
    *,
    lp_id: str = "lp_1",
    commitment: float = 1_000_000.0,
    called: float = 500_000.0,
    fraction: float = 0.10,
) -> LPCommitment:
    return LPCommitment(
        lp_id=lp_id,
        legal_name="LP One LLC",
        commitment_usd=commitment,
        called_to_date_usd=called,
        ownership_fraction=fraction,
    )


# ---------------------------------------------------------------------------
# NAV math
# ---------------------------------------------------------------------------


def test_compute_nav_single_position_signed_mark() -> None:
    commitment = _commit()
    positions = [
        PortfolioPosition(
            application_id="app_1",
            company_name="AcmeCo",
            cost_basis_usd=2_000_000.0,
            instrument_type="safe_post_money",
            invested_at=date(2026, 1, 15),
        )
    ]
    marks = {"app_1": _signed_mark("app_1", 4_000_000.0)}
    flows = [CashFlow(date(2026, 1, 10), -500_000.0, "capital_call")]

    nav = compute_nav(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=flows,
        as_of=date(2026, 3, 31),
    )

    assert nav.lp_id == "lp_1"
    assert nav.total_cost_basis_usd == pytest.approx(200_000.0)
    assert nav.total_fmv_usd == pytest.approx(400_000.0)
    assert nav.unrealized_gain_usd == pytest.approx(200_000.0)
    # NAV = LP-share FMV + uncalled = 400_000 + 500_000 = 900_000
    assert nav.nav_usd == pytest.approx(900_000.0)
    assert nav.uncalled_capital_usd == pytest.approx(500_000.0)
    # One position row produced.
    assert len(nav.positions) == 1
    pos = nav.positions[0]
    assert pos.lp_cost_basis_usd == pytest.approx(200_000.0)
    assert pos.lp_fmv_usd == pytest.approx(400_000.0)


def test_compute_nav_multiple_positions_sum_correctly() -> None:
    commitment = _commit(commitment=10_000_000.0, called=5_000_000.0, fraction=0.25)
    positions = [
        PortfolioPosition("app_a", "Alpha", 4_000_000.0),
        PortfolioPosition("app_b", "Beta", 6_000_000.0),
        # zero-cost position is silently skipped (not a held position)
        PortfolioPosition("app_c", "Gamma", 0.0),
    ]
    marks = {
        "app_a": _signed_mark("app_a", 8_000_000.0),
        "app_b": _signed_mark("app_b", 3_000_000.0),
    }
    nav = compute_nav(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=(),
        as_of=date(2026, 3, 31),
    )
    # LP-share cost: 0.25 * (4M + 6M) = 2.5M; LP-share FMV: 0.25 * (8M + 3M) = 2.75M
    assert nav.total_cost_basis_usd == pytest.approx(2_500_000.0)
    assert nav.total_fmv_usd == pytest.approx(2_750_000.0)
    assert nav.nav_usd == pytest.approx(2_750_000.0 + 5_000_000.0)
    assert len(nav.positions) == 2  # zero-cost row excluded


def test_unsigned_mark_blocks_publication() -> None:
    commitment = _commit()
    positions = [PortfolioPosition("app_1", "AcmeCo", 1_000_000.0)]
    unsigned = Mark(
        application_id="app_1",
        fmv_usd=2_000_000.0,
        as_of_date=date(2026, 3, 31),
        methodology="manager_mark",
        source="qoq_pulse",
        operator_signoff_at=None,
        operator_id="",
    )
    with pytest.raises(UnsignedMarkError):
        compute_nav(
            commitment=commitment,
            positions=positions,
            marks={"app_1": unsigned},
            cash_flows=(),
            as_of=date(2026, 3, 31),
        )


def test_missing_mark_blocks_publication() -> None:
    commitment = _commit()
    positions = [PortfolioPosition("app_1", "AcmeCo", 1_000_000.0)]
    with pytest.raises(UnsignedMarkError):
        compute_nav(
            commitment=commitment,
            positions=positions,
            marks={},
            cash_flows=(),
            as_of=date(2026, 3, 31),
        )


def test_invalid_inputs_are_rejected() -> None:
    bad_fraction = LPCommitment(
        lp_id="lp_x",
        legal_name="X",
        commitment_usd=100.0,
        called_to_date_usd=50.0,
        ownership_fraction=1.5,
    )
    with pytest.raises(InvalidInputError):
        compute_nav(
            commitment=bad_fraction,
            positions=(),
            marks={},
            cash_flows=(),
            as_of=date(2026, 3, 31),
        )

    over_called = LPCommitment(
        lp_id="lp_x",
        legal_name="X",
        commitment_usd=100.0,
        called_to_date_usd=200.0,
        ownership_fraction=0.1,
    )
    with pytest.raises(InvalidInputError):
        compute_nav(
            commitment=over_called,
            positions=(),
            marks={},
            cash_flows=(),
            as_of=date(2026, 3, 31),
        )

    with pytest.raises(InvalidInputError):
        compute_nav(
            commitment=_commit(),
            positions=(),
            marks={},
            cash_flows=[CashFlow(date(2026, 1, 1), 100.0, "capital_call")],
            as_of=date(2026, 3, 31),
        )


# ---------------------------------------------------------------------------
# IRR
# ---------------------------------------------------------------------------


def test_irr_recovers_known_doubling_in_one_year() -> None:
    flows = [
        CashFlow(date(2025, 1, 1), -1_000_000.0, "capital_call"),
    ]
    irr = compute_irr(flows, residual_nav_usd=2_000_000.0, as_of=date(2026, 1, 1))
    assert irr is not None
    # 100% return over one year (give or take leap-year rounding).
    assert irr == pytest.approx(1.0, rel=0.01)


def test_irr_zero_when_residual_equals_called() -> None:
    flows = [CashFlow(date(2025, 1, 1), -1_000_000.0, "capital_call")]
    irr = compute_irr(flows, residual_nav_usd=1_000_000.0, as_of=date(2026, 1, 1))
    assert irr is not None
    assert abs(irr) < 1e-6


def test_irr_none_for_degenerate_series() -> None:
    # All-positive flows have no IRR.
    only_distributions = [CashFlow(date(2025, 1, 1), 50.0, "distribution")]
    assert (
        compute_irr(only_distributions, residual_nav_usd=10.0, as_of=date(2026, 1, 1))
        is None
    )
    # Single-flow series with no residual.
    single = [CashFlow(date(2025, 1, 1), -100.0, "capital_call")]
    assert compute_irr(single, residual_nav_usd=0.0, as_of=date(2026, 1, 1)) is None


def test_irr_handles_capital_call_then_distribution_then_residual() -> None:
    flows = [
        CashFlow(date(2024, 1, 1), -1_000_000.0, "capital_call"),
        CashFlow(date(2025, 1, 1), 500_000.0, "distribution"),
    ]
    irr = compute_irr(flows, residual_nav_usd=1_500_000.0, as_of=date(2026, 1, 1))
    assert irr is not None
    # Hand-checked value: this series has an IRR around 50% per year.
    assert 0.3 < irr < 0.7


def test_compute_nav_sets_irr_field() -> None:
    commitment = _commit()
    positions = [PortfolioPosition("app_1", "AcmeCo", 2_000_000.0)]
    marks = {"app_1": _signed_mark("app_1", 8_000_000.0)}
    flows = [CashFlow(date(2025, 1, 1), -200_000.0, "capital_call")]
    nav = compute_nav(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=flows,
        as_of=date(2026, 1, 1),
    )
    assert nav.irr is not None
    # Cash in: -200k; residual NAV (LP share) = 800k; over ~1 year.
    assert math.isfinite(nav.irr)
