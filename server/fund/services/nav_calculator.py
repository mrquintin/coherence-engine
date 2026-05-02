"""Per-LP NAV calculator (prompt 69).

Computes a single LP's quarterly net-asset-value snapshot from three
inputs:

* the LP's :class:`LPCommitment` (committed capital + ownership %);
* the fund's portfolio of :class:`PortfolioPosition` rows
  (per-application cost basis at the fund level); and
* the operator-attested :class:`Mark` records that mark each
  position to fair value at the period close.

The output is a deterministic :class:`NAVSnapshot` dataclass plus an
IRR computation derived from the LP's signed cash flows
(commitments + capital calls out, distributions in, residual NAV in
as a synthetic terminal flow).

Math (per LP)
-------------

For each portfolio position::

    lp_cost_basis_i = position.cost_basis_usd * lp.ownership_fraction
    lp_fmv_i        = mark.fmv_usd            * lp.ownership_fraction

The LP-level totals are the sum across positions::

    total_cost_basis = sum(lp_cost_basis_i)
    total_fmv        = sum(lp_fmv_i)
    unrealized_gain  = total_fmv - total_cost_basis
    nav_usd          = total_fmv + lp_uncalled_capital_usd

IRR
---

We compute the LP's cash-on-cash IRR from the dated cash-flow series
the LP has experienced:

* capital calls (negative from the LP's perspective);
* distributions (positive); and
* the residual NAV at the period close (positive synthetic flow on
  the snapshot's ``as_of_date``).

If ``pyxirr`` is importable we delegate to it (it is fast and
battle-tested for sparse, irregular flows). Otherwise we fall back
to a Newton-Raphson search bracketed to ``[-0.999, 10.0]`` annual
returns. Both paths return ``None`` when the cash-flow series is
degenerate (all-positive or all-negative).

Mark gate (prompt 69 prohibition)
---------------------------------

Every position with a non-zero LP-level cost basis MUST have an
attached :class:`Mark` carrying ``operator_signoff_at`` set. A
missing or unsigned mark raises :class:`UnsignedMarkError`; the
caller is expected to surface this up the LP-reporting pipeline
rather than silently treat the position as marked-to-cost.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Mapping, Optional, Sequence


__all__ = [
    "LPCommitment",
    "PortfolioPosition",
    "Mark",
    "CashFlow",
    "PositionSnapshot",
    "NAVSnapshot",
    "NAVCalculatorError",
    "UnsignedMarkError",
    "InvalidInputError",
    "compute_nav",
    "compute_irr",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NAVCalculatorError(Exception):
    """Base error for the NAV calculator."""


class UnsignedMarkError(NAVCalculatorError):
    """Raised when a position lacks an operator-signed Mark.

    Quarterly statements MUST NOT publish unless every position with
    cost basis has a signed Mark — otherwise the LP would receive a
    statement whose FMV column is implicitly marked-to-cost without
    operator attestation.
    """


class InvalidInputError(NAVCalculatorError):
    """Raised on structural / value errors in the inputs (e.g. negative cost basis)."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LPCommitment:
    """A single LP's commitment to the fund.

    ``ownership_fraction`` is the LP's pro-rata share of the fund —
    typically ``commitment_usd / total_fund_commitments_usd`` — and
    is supplied by the caller rather than recomputed here so that the
    NAV calculator does not need to know the full LP roster. Values
    are floats in ``[0.0, 1.0]``.
    """

    lp_id: str
    legal_name: str
    commitment_usd: float
    called_to_date_usd: float
    ownership_fraction: float

    @property
    def uncalled_capital_usd(self) -> float:
        return max(0.0, float(self.commitment_usd) - float(self.called_to_date_usd))


@dataclass(frozen=True)
class PortfolioPosition:
    """A fund-level portfolio position.

    The ``cost_basis_usd`` is the fund's all-in cost (including any
    follow-on rounds that have already settled). FMV is *not* on this
    record — it lives on the matching :class:`Mark`.
    """

    application_id: str
    company_name: str
    cost_basis_usd: float
    instrument_type: str = "safe_post_money"
    invested_at: Optional[date] = None


@dataclass(frozen=True)
class Mark:
    """Operator-attested fair-market-value mark for a position.

    A mark is *active* only when ``operator_signoff_at`` is set; the
    NAV calculator treats an unsigned mark as missing. The
    ``methodology`` and ``source`` fields are carried verbatim into
    the LP statement so the LP can see whether the mark came from a
    priced round, a comp, or a manager mark.
    """

    application_id: str
    fmv_usd: float
    as_of_date: date
    methodology: str
    source: str
    operator_signoff_at: Optional[datetime] = None
    operator_id: str = ""
    note: str = ""

    @property
    def is_signed(self) -> bool:
        return self.operator_signoff_at is not None and bool(self.operator_id)


@dataclass(frozen=True)
class CashFlow:
    """A single dated cash flow at the LP level.

    Sign convention: outflows from the LP (capital calls) are
    NEGATIVE; inflows to the LP (distributions) are POSITIVE.
    """

    flow_date: date
    amount_usd: float
    kind: str  # "capital_call" | "distribution"


@dataclass(frozen=True)
class PositionSnapshot:
    """Per-position view of an LP's economic interest at the snapshot date."""

    application_id: str
    company_name: str
    instrument_type: str
    fund_cost_basis_usd: float
    fund_fmv_usd: float
    lp_cost_basis_usd: float
    lp_fmv_usd: float
    lp_unrealized_gain_usd: float
    mark_methodology: str
    mark_source: str
    mark_as_of: date


@dataclass(frozen=True)
class NAVSnapshot:
    """Frozen NAV snapshot for one LP at one period-close date."""

    lp_id: str
    legal_name: str
    as_of_date: date
    commitment_usd: float
    called_to_date_usd: float
    uncalled_capital_usd: float
    distributions_to_date_usd: float
    ownership_fraction: float
    total_cost_basis_usd: float
    total_fmv_usd: float
    unrealized_gain_usd: float
    nav_usd: float
    irr: Optional[float]
    positions: Sequence[PositionSnapshot] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_nav(
    *,
    commitment: LPCommitment,
    positions: Sequence[PortfolioPosition],
    marks: Mapping[str, Mark],
    cash_flows: Sequence[CashFlow],
    as_of: date,
) -> NAVSnapshot:
    """Compute one LP's NAV snapshot at ``as_of``.

    Raises :class:`UnsignedMarkError` if any position with non-zero
    LP cost basis has no signed Mark in ``marks``.
    """

    _validate_inputs(commitment, positions, cash_flows)
    fraction = float(commitment.ownership_fraction)

    rows: List[PositionSnapshot] = []
    total_cost = 0.0
    total_fmv = 0.0

    # Sorting deterministically by application_id keeps the rendered
    # statement byte-stable across reruns with the same inputs — load
    # bearing for the deterministic-PDF test.
    for position in sorted(positions, key=lambda p: p.application_id):
        lp_cost = float(position.cost_basis_usd) * fraction
        if lp_cost <= 0.0:
            continue

        mark = marks.get(position.application_id)
        if mark is None or not mark.is_signed:
            raise UnsignedMarkError(
                f"position {position.application_id} ({position.company_name}) "
                "has no operator-signed Mark — cannot publish statement"
            )

        lp_fmv = float(mark.fmv_usd) * fraction
        rows.append(
            PositionSnapshot(
                application_id=position.application_id,
                company_name=position.company_name,
                instrument_type=position.instrument_type,
                fund_cost_basis_usd=float(position.cost_basis_usd),
                fund_fmv_usd=float(mark.fmv_usd),
                lp_cost_basis_usd=lp_cost,
                lp_fmv_usd=lp_fmv,
                lp_unrealized_gain_usd=lp_fmv - lp_cost,
                mark_methodology=mark.methodology,
                mark_source=mark.source,
                mark_as_of=mark.as_of_date,
            )
        )
        total_cost += lp_cost
        total_fmv += lp_fmv

    distributions_total = sum(
        float(c.amount_usd) for c in cash_flows if c.kind == "distribution"
    )
    nav_usd = total_fmv + commitment.uncalled_capital_usd

    irr = compute_irr(cash_flows, residual_nav_usd=total_fmv, as_of=as_of)

    return NAVSnapshot(
        lp_id=commitment.lp_id,
        legal_name=commitment.legal_name,
        as_of_date=as_of,
        commitment_usd=float(commitment.commitment_usd),
        called_to_date_usd=float(commitment.called_to_date_usd),
        uncalled_capital_usd=commitment.uncalled_capital_usd,
        distributions_to_date_usd=distributions_total,
        ownership_fraction=fraction,
        total_cost_basis_usd=total_cost,
        total_fmv_usd=total_fmv,
        unrealized_gain_usd=total_fmv - total_cost,
        nav_usd=nav_usd,
        irr=irr,
        positions=tuple(rows),
    )


def compute_irr(
    cash_flows: Sequence[CashFlow],
    *,
    residual_nav_usd: float,
    as_of: date,
) -> Optional[float]:
    """Compute the LP's annualised IRR over the supplied cash flows.

    The residual NAV is appended as a synthetic POSITIVE flow on
    ``as_of`` so the function answers the standard "what if the LP
    were to liquidate today" IRR. Returns ``None`` for degenerate
    series (no sign change → no real IRR).
    """

    flows: List[tuple[date, float]] = [
        (c.flow_date, float(c.amount_usd)) for c in cash_flows
    ]
    if residual_nav_usd > 0.0:
        flows.append((as_of, float(residual_nav_usd)))
    if len(flows) < 2:
        return None

    has_positive = any(amount > 0 for _, amount in flows)
    has_negative = any(amount < 0 for _, amount in flows)
    if not (has_positive and has_negative):
        return None

    flows.sort(key=lambda fa: fa[0])

    # Try pyxirr first when it is installed; the wheel ships fast,
    # battle-tested code for sparse irregular flows. Fall through to
    # the in-tree Newton solver when it is missing.
    try:  # pragma: no cover - presence depends on the runtime env
        import pyxirr  # type: ignore

        dates_ = [d for d, _ in flows]
        amounts_ = [a for _, a in flows]
        result = pyxirr.xirr(dates_, amounts_)
        if result is None:
            return None
        try:
            value = float(result)
        except (TypeError, ValueError):
            return None
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    except ImportError:
        return _newton_xirr(flows)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_inputs(
    commitment: LPCommitment,
    positions: Sequence[PortfolioPosition],
    cash_flows: Sequence[CashFlow],
) -> None:
    if commitment.commitment_usd < 0:
        raise InvalidInputError("commitment_usd must be non-negative")
    if commitment.called_to_date_usd < 0:
        raise InvalidInputError("called_to_date_usd must be non-negative")
    if commitment.called_to_date_usd > commitment.commitment_usd:
        raise InvalidInputError(
            "called_to_date_usd must not exceed commitment_usd"
        )
    if not 0.0 <= commitment.ownership_fraction <= 1.0:
        raise InvalidInputError(
            "ownership_fraction must lie in [0.0, 1.0]"
        )
    for position in positions:
        if position.cost_basis_usd < 0:
            raise InvalidInputError(
                f"position {position.application_id} has negative cost basis"
            )
    for flow in cash_flows:
        if flow.kind not in {"capital_call", "distribution"}:
            raise InvalidInputError(
                f"cash flow kind {flow.kind!r} is not recognised"
            )
        if flow.kind == "capital_call" and flow.amount_usd > 0:
            raise InvalidInputError(
                "capital_call flows must be NEGATIVE from the LP's perspective"
            )
        if flow.kind == "distribution" and flow.amount_usd < 0:
            raise InvalidInputError(
                "distribution flows must be POSITIVE from the LP's perspective"
            )


def _xirr_npv(rate: float, flows: Sequence[tuple[date, float]]) -> float:
    """Net-present-value of dated flows at the supplied annual rate.

    The implementation matches the convention pyxirr / Excel use:
    each flow is discounted by ``(1 + rate) ** (days / 365.0)``.
    """

    if rate <= -1.0:
        return float("inf")
    base_date = flows[0][0]
    total = 0.0
    for d, amount in flows:
        years = (d - base_date).days / 365.0
        total += amount / ((1.0 + rate) ** years)
    return total


def _xirr_npv_derivative(
    rate: float, flows: Sequence[tuple[date, float]]
) -> float:
    if rate <= -1.0:
        return float("inf")
    base_date = flows[0][0]
    total = 0.0
    for d, amount in flows:
        years = (d - base_date).days / 365.0
        if years == 0.0:
            continue
        total += -years * amount / ((1.0 + rate) ** (years + 1.0))
    return total


def _newton_xirr(
    flows: Sequence[tuple[date, float]],
    *,
    guess: float = 0.1,
    max_iter: int = 128,
    tol: float = 1e-7,
) -> Optional[float]:
    """Newton-Raphson XIRR with a bisection fallback.

    Newton converges fast for well-behaved series but can wander on
    pathological flows; we cap iterations and fall back to bisection
    over ``[-0.999, 10.0]`` if Newton fails to settle.
    """

    rate = guess
    for _ in range(max_iter):
        npv = _xirr_npv(rate, flows)
        if abs(npv) < tol:
            if math.isnan(rate) or math.isinf(rate):
                return None
            return rate
        deriv = _xirr_npv_derivative(rate, flows)
        if deriv == 0.0:
            break
        next_rate = rate - npv / deriv
        if next_rate <= -1.0:
            next_rate = (rate - 1.0) / 2.0  # clamp away from -100%
        if abs(next_rate - rate) < tol:
            rate = next_rate
            if math.isnan(rate) or math.isinf(rate):
                return None
            return rate
        rate = next_rate

    # Bisection fallback.
    low, high = -0.999, 10.0
    f_low = _xirr_npv(low, flows)
    f_high = _xirr_npv(high, flows)
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2.0
        f_mid = _xirr_npv(mid, flows)
        if abs(f_mid) < tol:
            return mid
        if f_low * f_mid < 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
    return (low + high) / 2.0
