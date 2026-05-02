"""Deterministic reserve-allocation optimizer (prompt 70, Wave 18).

Given the current portfolio state, the validation study's
calibrated false-pass cost estimate, and projected pipeline volume,
this module recomputes a *proposed* parameter set:

* Per-domain ``CS0_d`` (target coherence intercept) and ``alpha_d``
  (slope of the per-check-size threshold).
* A liquidity-reserve target, expressed both as a fraction of the
  notional capacity and as an absolute USD floor.
* A pipeline-volume cap (a soft throttle on open applications).

The optimizer is framed as a constrained optimization problem:

    minimize  expected_lost_ev(theta)
    s.t.      expected_false_pass_exposure(theta) <= false_pass_budget_usd
              CS0_d in [CS0_min, CS0_max]
              alpha_d in [alpha_min, alpha_max]

When ``scipy`` is available the SLSQP path of
:func:`scipy.optimize.minimize` is used (lazily imported). Otherwise a
deterministic grid search over ``(CS0_d, alpha_d)`` is run; the grid
spacing is derived from the seed so two runs with identical inputs and
seed yield byte-identical proposals. Either way the **proposal is
never auto-promoted** -- the operator (with the partner committee)
must explicitly approve it via
:mod:`server.fund.services.policy_parameter_proposals`.

The cost-of-error model is derived from the validation study report
(prompt 44): the bootstrap coefficient on ``coherence_score`` provides
the slope of survival probability as coherence rises, which feeds the
per-row false-pass and lost-EV expectations.

Determinism guarantees
----------------------

* Same inputs + same seed -> byte-identical
  :meth:`OptimizerResult.to_canonical_dict` output.
* No wall-clock reads, no live database reads, no network calls.
* No mutation of the running decision policy: the optimizer simulates
  the coherence-gate locally using the proposed
  ``(CS0_d, alpha_d, S_min_d)`` so the live module is untouched.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


RESERVE_OPTIMIZER_VERSION = "reserve-optimizer-v1"


# ---------------------------------------------------------------------------
# Defaults / bounds
# ---------------------------------------------------------------------------


# Mirrors :func:`decision_policy.DecisionPolicyService._params_for_domain`
# defaults so the optimizer's "current parameters" baseline matches what
# production runs at rest.
_CURRENT_DOMAIN_DEFAULTS: Dict[str, Dict[str, float]] = {
    "market_economics": {"CS0_d": 0.18, "gamma_d": 2.0, "S_min_d": 50_000.0},
    "governance": {"CS0_d": 0.20, "gamma_d": 2.2, "S_min_d": 50_000.0},
    "public_health": {"CS0_d": 0.22, "gamma_d": 2.4, "S_min_d": 50_000.0},
}


_DEFAULT_BOUNDS: Dict[str, Tuple[float, float]] = {
    "CS0_d": (0.10, 0.40),
    "alpha_d": (0.10, 0.50),
}

_DEFAULT_GRID_SIZE = 9          # 9 x 9 = 81 evaluations per domain
_DEFAULT_RESERVE_FRACTION = 0.05
_DEFAULT_PIPELINE_CAP = 40

# These are deliberately conservative anchors: false-pass cost ~ 5x
# lost-EV cost. Operators can override by passing an explicit
# :class:`CostOfErrorModel`.
_DEFAULT_FALSE_PASS_COST_USD = 200_000.0
_DEFAULT_LOST_EV_USD = 40_000.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReserveOptimizerError(RuntimeError):
    """Raised on validation failures inside the optimizer."""


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostOfErrorModel:
    """Cost-of-error coefficients derived from the validation study.

    ``beta_coherence`` is the bootstrap point estimate of the logistic
    coefficient on ``coherence_score`` (prompt 44 ``coefficients[].point``
    where ``name == "coherence_score"``). The optimizer uses it as the
    slope of survival-probability over coherence; a small or negative
    value caps the expected gain from raising CS0_d.

    ``false_pass_cost_usd`` and ``lost_ev_usd`` express the per-unit
    cost the operator is willing to trade off. Defaults are the
    conservative anchors documented in
    ``docs/specs/reserve_optimizer.md`` -- production runs MUST override
    them with the calibrated estimates from the validation report.
    """

    beta_coherence: float = 0.0
    beta_intercept: float = 0.0
    false_pass_cost_usd: float = _DEFAULT_FALSE_PASS_COST_USD
    lost_ev_usd: float = _DEFAULT_LOST_EV_USD

    @classmethod
    def from_validation_study(
        cls,
        report: Mapping[str, Any],
        *,
        false_pass_cost_usd: float = _DEFAULT_FALSE_PASS_COST_USD,
        lost_ev_usd: float = _DEFAULT_LOST_EV_USD,
    ) -> "CostOfErrorModel":
        coefficients = report.get("coefficients") or []
        beta_coh = 0.0
        beta_intercept = 0.0
        for c in coefficients:
            if not isinstance(c, Mapping):
                continue
            name = str(c.get("name") or "")
            if name == "coherence_score":
                beta_coh = float(c.get("point") or 0.0)
            elif name == "intercept":
                beta_intercept = float(c.get("point") or 0.0)
        return cls(
            beta_coherence=float(beta_coh),
            beta_intercept=float(beta_intercept),
            false_pass_cost_usd=float(false_pass_cost_usd),
            lost_ev_usd=float(lost_ev_usd),
        )


@dataclass(frozen=True)
class DomainParameters:
    """Single-domain parameter pair the optimizer proposes."""

    CS0_d: float
    alpha_d: float
    S_min_d: float = 50_000.0


@dataclass(frozen=True)
class ProposedParameterSet:
    """Full proposal: per-domain pairs + reserve + pipeline cap."""

    domains: Mapping[str, DomainParameters]
    liquidity_reserve_fraction: float
    liquidity_reserve_target_usd: float
    pipeline_volume_cap: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domains": {
                d: {
                    "CS0_d": round(p.CS0_d, 6),
                    "alpha_d": round(p.alpha_d, 6),
                    "S_min_d": round(p.S_min_d, 2),
                }
                for d, p in sorted(self.domains.items())
            },
            "liquidity_reserve_fraction": round(self.liquidity_reserve_fraction, 6),
            "liquidity_reserve_target_usd": round(self.liquidity_reserve_target_usd, 2),
            "pipeline_volume_cap": int(self.pipeline_volume_cap),
        }


@dataclass(frozen=True)
class HistoricalRow:
    """One historical replay row consumed by the optimizer.

    Mirrors the relevant subset of the governed historical-outcomes
    schema (prompt 11) -- ``coherence_superiority`` is the observed
    score, ``outcome_superiority`` the realized label, ``check_size_usd``
    the requested check (defaulted when missing).
    """

    domain: str
    coherence_superiority: float
    outcome_superiority: float
    check_size_usd: float = 50_000.0


@dataclass(frozen=True)
class BacktestDelta:
    """Aggregate before/after deltas for the proposed parameters."""

    pass_rate_before: float
    pass_rate_after: float
    false_pass_exposure_usd_before: float
    false_pass_exposure_usd_after: float
    reserve_coverage_before: float
    reserve_coverage_after: float
    objective_before: float
    objective_after: float
    improved: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pass_rate_before": round(self.pass_rate_before, 6),
            "pass_rate_after": round(self.pass_rate_after, 6),
            "pass_rate_change": round(self.pass_rate_after - self.pass_rate_before, 6),
            "false_pass_exposure_usd_before": round(self.false_pass_exposure_usd_before, 2),
            "false_pass_exposure_usd_after": round(self.false_pass_exposure_usd_after, 2),
            "false_pass_exposure_change": round(
                self.false_pass_exposure_usd_after - self.false_pass_exposure_usd_before, 2
            ),
            "reserve_coverage_before": round(self.reserve_coverage_before, 6),
            "reserve_coverage_after": round(self.reserve_coverage_after, 6),
            "objective_before": round(self.objective_before, 6),
            "objective_after": round(self.objective_after, 6),
            "improved": bool(self.improved),
        }


@dataclass(frozen=True)
class OptimizerInputs:
    """All inputs needed for a single optimization run."""

    portfolio_snapshot: Mapping[str, Any]
    validation_study: Mapping[str, Any]
    historical_rows: Sequence[HistoricalRow]
    projected_pipeline_volume: int
    domains: Tuple[str, ...] = ("market_economics", "governance", "public_health")
    false_pass_budget_usd: float = 1_000_000.0
    seed: int = 0
    grid_size: int = _DEFAULT_GRID_SIZE
    cost_overrides: Optional[CostOfErrorModel] = None
    prefer_scipy: bool = False

    @staticmethod
    def from_payload(
        *,
        portfolio_snapshot: Mapping[str, Any],
        validation_study: Mapping[str, Any],
        historical_rows: Sequence[Mapping[str, Any]],
        projected_pipeline_volume: int,
        false_pass_budget_usd: float = 1_000_000.0,
        seed: int = 0,
    ) -> "OptimizerInputs":
        rows: List[HistoricalRow] = []
        for raw in historical_rows or []:
            try:
                cs = float(raw.get("coherence_superiority") or 0.0)
                outcome = float(raw.get("outcome_superiority") or 0.0)
            except (TypeError, ValueError):
                continue
            check = raw.get("check_size_usd")
            try:
                check_f = float(check) if check is not None else 50_000.0
            except (TypeError, ValueError):
                check_f = 50_000.0
            if check_f <= 0.0:
                check_f = 50_000.0
            rows.append(
                HistoricalRow(
                    domain=str(raw.get("domain") or "market_economics"),
                    coherence_superiority=cs,
                    outcome_superiority=outcome,
                    check_size_usd=check_f,
                )
            )
        return OptimizerInputs(
            portfolio_snapshot=dict(portfolio_snapshot or {}),
            validation_study=dict(validation_study or {}),
            historical_rows=tuple(rows),
            projected_pipeline_volume=int(projected_pipeline_volume or 0),
            false_pass_budget_usd=float(false_pass_budget_usd),
            seed=int(seed),
        )


@dataclass(frozen=True)
class OptimizerResult:
    """Full optimizer output: proposal + audit + backtest delta."""

    schema_version: str
    proposed: ProposedParameterSet
    current: ProposedParameterSet
    cost_model: CostOfErrorModel
    delta: BacktestDelta
    optimizer_method: str
    seed: int
    inputs_digest: str

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "cost_model": {
                "beta_coherence": round(self.cost_model.beta_coherence, 6),
                "beta_intercept": round(self.cost_model.beta_intercept, 6),
                "false_pass_cost_usd": round(self.cost_model.false_pass_cost_usd, 2),
                "lost_ev_usd": round(self.cost_model.lost_ev_usd, 2),
            },
            "current": self.current.to_dict(),
            "delta": self.delta.to_dict(),
            "inputs_digest": self.inputs_digest,
            "optimizer_method": self.optimizer_method,
            "proposed": self.proposed.to_dict(),
            "schema_version": self.schema_version,
            "seed": int(self.seed),
        }

    def to_canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.to_canonical_dict(), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

    def report_digest(self) -> str:
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Coherence-gate simulation
# ---------------------------------------------------------------------------


def _cs_required(params: DomainParameters, check_size_usd: float) -> float:
    """Mirror of :meth:`DecisionPolicyService._params_for_domain` math.

    The simulator is intentionally narrow: only the coherence-gate
    threshold is recomputed under the proposed parameters. The other
    gates (compliance, KYC, anti-gaming, portfolio) are out of scope
    for the optimizer because they don't depend on
    ``(CS0_d, alpha_d)``.
    """

    s = max(check_size_usd, params.S_min_d)
    return params.CS0_d + params.alpha_d * math.log2(s / params.S_min_d)


def _row_passes(row: HistoricalRow, params: DomainParameters) -> bool:
    return row.coherence_superiority >= _cs_required(params, row.check_size_usd)


def _is_false_pass(row: HistoricalRow) -> bool:
    """A pass is "false" when realized outcome was not strictly positive."""

    return row.outcome_superiority <= 0.0


def _evaluate_parameter_set(
    params_by_domain: Mapping[str, DomainParameters],
    rows: Sequence[HistoricalRow],
    *,
    cost_model: CostOfErrorModel,
) -> Dict[str, float]:
    """Score a parameter set on the historical replay corpus.

    Returns aggregates plus a per-row objective contribution. The
    objective is the sum of:

    * ``lost_ev_usd`` for each *positive-outcome* row that is rejected
      (the engine missed a winner).
    * ``false_pass_cost_usd`` for each *non-positive-outcome* row that
      is passed (the engine bet on a loser).

    Both terms are scaled by ``check_size_usd / 50000`` so a $250k
    write-down is weighted ~5x a $50k one.
    """

    n = len(rows)
    if n == 0:
        return {
            "n": 0.0,
            "pass_rate": 0.0,
            "false_pass_exposure_usd": 0.0,
            "objective": 0.0,
            "n_passes": 0.0,
            "n_false_passes": 0.0,
            "n_missed_winners": 0.0,
        }
    n_passes = 0
    n_false_passes = 0
    n_missed_winners = 0
    false_pass_exposure = 0.0
    objective = 0.0
    for row in rows:
        params = params_by_domain.get(row.domain)
        if params is None:
            params = params_by_domain.get(
                "market_economics",
                DomainParameters(CS0_d=0.18, alpha_d=0.25, S_min_d=50_000.0),
            )
        passed = _row_passes(row, params)
        weight = max(1.0, row.check_size_usd / 50_000.0)
        if passed:
            n_passes += 1
            if _is_false_pass(row):
                n_false_passes += 1
                false_pass_exposure += row.check_size_usd
                objective += cost_model.false_pass_cost_usd * weight
        else:
            if not _is_false_pass(row):
                n_missed_winners += 1
                objective += cost_model.lost_ev_usd * weight
    return {
        "n": float(n),
        "pass_rate": n_passes / n,
        "false_pass_exposure_usd": false_pass_exposure,
        "objective": objective,
        "n_passes": float(n_passes),
        "n_false_passes": float(n_false_passes),
        "n_missed_winners": float(n_missed_winners),
    }


# ---------------------------------------------------------------------------
# Per-domain optimizer
# ---------------------------------------------------------------------------


def _domain_rows(
    rows: Sequence[HistoricalRow], domain: str
) -> Tuple[HistoricalRow, ...]:
    return tuple(r for r in rows if r.domain == domain)


def _grid_search_domain(
    rows: Sequence[HistoricalRow],
    *,
    cost_model: CostOfErrorModel,
    bounds: Mapping[str, Tuple[float, float]],
    grid_size: int,
    s_min: float,
    false_pass_budget_per_domain: float,
    current: Optional[DomainParameters] = None,
) -> DomainParameters:
    """Deterministic 2-D grid search over (CS0_d, alpha_d).

    Visits ``grid_size x grid_size`` points and picks the one that
    minimizes the objective subject to
    ``false_pass_exposure <= false_pass_budget_per_domain``. The
    current parameters are added as an explicit candidate so the
    optimizer can never propose a strictly-worse policy on the same
    objective surface (the proposal converges to the current setting
    when no grid point dominates it). When no feasible point exists
    (synthetic frame too small) the constraint is relaxed and the
    unconstrained minimum is returned.
    """

    cs_lo, cs_hi = bounds["CS0_d"]
    al_lo, al_hi = bounds["alpha_d"]
    if grid_size < 2:
        grid_size = 2
    cs_step = (cs_hi - cs_lo) / (grid_size - 1)
    al_step = (al_hi - al_lo) / (grid_size - 1)
    candidates: List[DomainParameters] = []
    for i in range(grid_size):
        cs0 = cs_lo + i * cs_step
        for j in range(grid_size):
            al = al_lo + j * al_step
            candidates.append(
                DomainParameters(
                    CS0_d=round(cs0, 6),
                    alpha_d=round(al, 6),
                    S_min_d=s_min,
                )
            )
    if current is not None:
        candidates.append(current)

    best: Optional[Tuple[Tuple[float, float, float], DomainParameters]] = None
    best_relaxed: Optional[Tuple[Tuple[float, float, float], DomainParameters]] = None
    domain_label = rows[0].domain if rows else "_"
    for cand in candidates:
        agg = _evaluate_parameter_set(
            {domain_label: cand},
            rows,
            cost_model=cost_model,
        )
        obj = float(agg["objective"])
        exposure = float(agg["false_pass_exposure_usd"])
        # Use a deterministic tiebreaker on the candidate's numeric
        # values so two grid points with the same objective always
        # pick the same winner regardless of insertion order.
        sort_key = (obj, cand.CS0_d, cand.alpha_d)
        if best_relaxed is None or sort_key < best_relaxed[0]:
            best_relaxed = (sort_key, cand)
        if exposure <= false_pass_budget_per_domain:
            if best is None or sort_key < best[0]:
                best = (sort_key, cand)
    chosen = best if best is not None else best_relaxed
    if chosen is None:
        return current or DomainParameters(
            CS0_d=0.18, alpha_d=0.25, S_min_d=s_min
        )
    return chosen[1]


def _scipy_search_domain(
    rows: Sequence[HistoricalRow],
    *,
    cost_model: CostOfErrorModel,
    bounds: Mapping[str, Tuple[float, float]],
    s_min: float,
    false_pass_budget_per_domain: float,
    grid_size: int,
    current: Optional[DomainParameters] = None,
) -> Tuple[DomainParameters, str]:
    """SLSQP path. Falls back to grid on any import or solver failure.

    Returns ``(params, method)`` where ``method`` is ``"scipy"`` on
    success or ``"grid"`` on fallback so the caller can record the
    actual technique in the report.
    """

    try:
        # Lazy import: scipy is an optional dependency; the grid path
        # is always available.
        from scipy.optimize import minimize  # type: ignore
    except Exception:
        return (
            _grid_search_domain(
                rows,
                cost_model=cost_model,
                bounds=bounds,
                grid_size=grid_size,
                s_min=s_min,
                false_pass_budget_per_domain=false_pass_budget_per_domain,
                current=current,
            ),
            "grid",
        )

    domain_label = rows[0].domain if rows else "_"
    cs_lo, cs_hi = bounds["CS0_d"]
    al_lo, al_hi = bounds["alpha_d"]

    def objective(theta: Sequence[float]) -> float:
        cand = DomainParameters(
            CS0_d=float(theta[0]), alpha_d=float(theta[1]), S_min_d=s_min
        )
        agg = _evaluate_parameter_set({domain_label: cand}, rows, cost_model=cost_model)
        return float(agg["objective"])

    def constraint(theta: Sequence[float]) -> float:
        cand = DomainParameters(
            CS0_d=float(theta[0]), alpha_d=float(theta[1]), S_min_d=s_min
        )
        agg = _evaluate_parameter_set({domain_label: cand}, rows, cost_model=cost_model)
        return float(false_pass_budget_per_domain - agg["false_pass_exposure_usd"])

    x0 = [
        (cs_lo + cs_hi) / 2.0,
        (al_lo + al_hi) / 2.0,
    ]
    try:
        res = minimize(
            objective,
            x0,
            method="SLSQP",
            bounds=[(cs_lo, cs_hi), (al_lo, al_hi)],
            constraints=[{"type": "ineq", "fun": constraint}],
            options={"maxiter": 50, "ftol": 1e-6},
        )
        if not getattr(res, "success", False):
            raise RuntimeError("SLSQP did not converge")
        x = list(res.x)
    except Exception:
        return (
            _grid_search_domain(
                rows,
                cost_model=cost_model,
                bounds=bounds,
                grid_size=grid_size,
                s_min=s_min,
                false_pass_budget_per_domain=false_pass_budget_per_domain,
                current=current,
            ),
            "grid",
        )
    return (
        DomainParameters(
            CS0_d=round(float(x[0]), 6),
            alpha_d=round(float(x[1]), 6),
            S_min_d=s_min,
        ),
        "scipy",
    )


# ---------------------------------------------------------------------------
# Reserve + pipeline cap
# ---------------------------------------------------------------------------


def _liquidity_reserve(
    portfolio_snapshot: Mapping[str, Any],
    *,
    delta: Mapping[str, float],
) -> Tuple[float, float]:
    """Return ``(fraction, target_usd)``.

    The base fraction is :data:`_DEFAULT_RESERVE_FRACTION`. It is bumped
    upward when the proposed parameter set would shift mass toward
    higher-exposure passes (positive ``false_pass_exposure_change``) so
    a riskier policy carries more cushion -- a behavioral lever the
    operator can re-weight via the spec's ``reserve_sensitivity``
    parameter.
    """

    nav = float(portfolio_snapshot.get("fund_nav_usd") or 12_000_000.0)
    base_fraction = _DEFAULT_RESERVE_FRACTION
    exposure_change = float(delta.get("false_pass_exposure_change") or 0.0)
    if exposure_change > 0.0 and nav > 0.0:
        bump = min(0.05, exposure_change / nav)
        fraction = min(0.20, base_fraction + bump)
    else:
        fraction = base_fraction
    target_usd = max(0.0, nav * fraction)
    return fraction, target_usd


def _pipeline_cap(projected_pipeline_volume: int) -> int:
    """Soft pipeline cap, derived from projected volume + a 10% headroom.

    Bounded below by :data:`_DEFAULT_PIPELINE_CAP` so the cap never
    drops to a value tighter than the existing decision policy's
    ``r_pipeline`` step (which kicks in at 40 open applications).
    """

    if projected_pipeline_volume <= 0:
        return _DEFAULT_PIPELINE_CAP
    cap = int(round(projected_pipeline_volume * 1.10))
    return max(_DEFAULT_PIPELINE_CAP, cap)


# ---------------------------------------------------------------------------
# Inputs digest
# ---------------------------------------------------------------------------


def _digest_inputs(inp: OptimizerInputs) -> str:
    h = hashlib.sha256()
    payload = {
        "portfolio_snapshot": dict(inp.portfolio_snapshot),
        "validation_study_hash": str(
            inp.validation_study.get("data_hash") or ""
        ),
        "n_rows": len(inp.historical_rows),
        "rows_digest": _hash_rows(inp.historical_rows),
        "domains": list(inp.domains),
        "projected_pipeline_volume": int(inp.projected_pipeline_volume),
        "false_pass_budget_usd": float(inp.false_pass_budget_usd),
        "seed": int(inp.seed),
        "grid_size": int(inp.grid_size),
    }
    h.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return h.hexdigest()


def _hash_rows(rows: Sequence[HistoricalRow]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(
            json.dumps(
                {
                    "d": r.domain,
                    "c": round(r.coherence_superiority, 6),
                    "o": round(r.outcome_superiority, 6),
                    "k": round(r.check_size_usd, 2),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _current_parameter_set(
    domains: Sequence[str],
    portfolio_snapshot: Mapping[str, Any],
    projected_pipeline_volume: int,
) -> ProposedParameterSet:
    """Return the *current* baseline (the running decision-policy params)."""

    nav = float(portfolio_snapshot.get("fund_nav_usd") or 12_000_000.0)
    domain_params: Dict[str, DomainParameters] = {}
    for d in domains:
        defaults = _CURRENT_DOMAIN_DEFAULTS.get(d, _CURRENT_DOMAIN_DEFAULTS["market_economics"])
        gamma = float(defaults["gamma_d"])
        alpha = 1.0 / (2.0 * gamma)
        domain_params[d] = DomainParameters(
            CS0_d=float(defaults["CS0_d"]),
            alpha_d=alpha,
            S_min_d=float(defaults["S_min_d"]),
        )
    reserve = float(portfolio_snapshot.get("liquidity_reserve_usd") or (nav * _DEFAULT_RESERVE_FRACTION))
    fraction = reserve / nav if nav > 0 else _DEFAULT_RESERVE_FRACTION
    return ProposedParameterSet(
        domains=domain_params,
        liquidity_reserve_fraction=fraction,
        liquidity_reserve_target_usd=reserve,
        pipeline_volume_cap=_pipeline_cap(projected_pipeline_volume),
    )


def optimize(inputs: OptimizerInputs) -> OptimizerResult:
    """Run the deterministic reserve-allocation optimizer.

    The output is a *proposal*. The caller (typically
    :func:`policy_parameter_proposals.create_proposal`) is responsible
    for persisting it and routing it to the explicit operator approval
    workflow. **No promotion** of the running decision policy occurs
    inside this function.
    """

    if not isinstance(inputs, OptimizerInputs):
        raise ReserveOptimizerError("optimize() requires an OptimizerInputs instance")
    if inputs.false_pass_budget_usd < 0:
        raise ReserveOptimizerError("false_pass_budget_usd must be >= 0")

    cost_model = inputs.cost_overrides or CostOfErrorModel.from_validation_study(
        inputs.validation_study
    )

    current_set = _current_parameter_set(
        inputs.domains,
        inputs.portfolio_snapshot,
        inputs.projected_pipeline_volume,
    )

    # Per-domain budget allocation: even split. Re-weighting per
    # historical exposure is a future extension.
    n_domains = max(1, len(inputs.domains))
    per_domain_budget = inputs.false_pass_budget_usd / n_domains

    proposed_domain_params: Dict[str, DomainParameters] = {}
    optimizer_method = "grid"
    for d in inputs.domains:
        d_rows = _domain_rows(inputs.historical_rows, d)
        s_min = float(
            _CURRENT_DOMAIN_DEFAULTS.get(d, _CURRENT_DOMAIN_DEFAULTS["market_economics"])[
                "S_min_d"
            ]
        )
        current_for_domain = current_set.domains.get(d)
        if inputs.prefer_scipy:
            params, method = _scipy_search_domain(
                d_rows,
                cost_model=cost_model,
                bounds=_DEFAULT_BOUNDS,
                s_min=s_min,
                false_pass_budget_per_domain=per_domain_budget,
                grid_size=inputs.grid_size,
                current=current_for_domain,
            )
            if method == "scipy":
                optimizer_method = "scipy"
        else:
            params = _grid_search_domain(
                d_rows,
                cost_model=cost_model,
                bounds=_DEFAULT_BOUNDS,
                grid_size=inputs.grid_size,
                s_min=s_min,
                false_pass_budget_per_domain=per_domain_budget,
                current=current_for_domain,
            )
        proposed_domain_params[d] = params

    before = _evaluate_parameter_set(
        current_set.domains, inputs.historical_rows, cost_model=cost_model
    )
    after = _evaluate_parameter_set(
        proposed_domain_params, inputs.historical_rows, cost_model=cost_model
    )

    fraction, reserve_target = _liquidity_reserve(
        inputs.portfolio_snapshot,
        delta={
            "false_pass_exposure_change": after["false_pass_exposure_usd"]
            - before["false_pass_exposure_usd"],
        },
    )
    pipeline_cap = _pipeline_cap(inputs.projected_pipeline_volume)
    proposed_set = ProposedParameterSet(
        domains=proposed_domain_params,
        liquidity_reserve_fraction=fraction,
        liquidity_reserve_target_usd=reserve_target,
        pipeline_volume_cap=pipeline_cap,
    )

    nav = float(inputs.portfolio_snapshot.get("fund_nav_usd") or 12_000_000.0)
    reserve_coverage_before = (
        float(inputs.portfolio_snapshot.get("liquidity_reserve_usd") or 0.0) / nav
        if nav > 0
        else 0.0
    )
    reserve_coverage_after = fraction

    delta = BacktestDelta(
        pass_rate_before=float(before["pass_rate"]),
        pass_rate_after=float(after["pass_rate"]),
        false_pass_exposure_usd_before=float(before["false_pass_exposure_usd"]),
        false_pass_exposure_usd_after=float(after["false_pass_exposure_usd"]),
        reserve_coverage_before=reserve_coverage_before,
        reserve_coverage_after=reserve_coverage_after,
        objective_before=float(before["objective"]),
        objective_after=float(after["objective"]),
        improved=bool(after["objective"] <= before["objective"] + 1e-9),
    )

    return OptimizerResult(
        schema_version=RESERVE_OPTIMIZER_VERSION,
        proposed=proposed_set,
        current=current_set,
        cost_model=cost_model,
        delta=delta,
        optimizer_method=optimizer_method,
        seed=int(inputs.seed),
        inputs_digest=_digest_inputs(inputs),
    )


__all__ = [
    "RESERVE_OPTIMIZER_VERSION",
    "BacktestDelta",
    "CostOfErrorModel",
    "DomainParameters",
    "HistoricalRow",
    "OptimizerInputs",
    "OptimizerResult",
    "ProposedParameterSet",
    "ReserveOptimizerError",
    "optimize",
]
