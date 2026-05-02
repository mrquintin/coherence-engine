"""Reserve-allocation optimizer tests (prompt 70).

Covers:

* On a synthetic fund + validation report, the optimizer outputs
  feasible parameters that improve the objective on the in-sample
  backtest replay (compared to the current decision-policy defaults).
* Determinism: two calls with the same inputs and seed produce
  byte-identical canonical reports.
* Backtest delta: ``before / after`` aggregates are populated with
  pass-rate, false-pass-exposure, reserve-coverage, and the
  ``improved`` boolean.
* Cost-of-error model derivation from a validation study report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.server.fund.services import reserve_optimizer as ro


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "policy"


def _load_inputs() -> ro.OptimizerInputs:
    payload = json.loads((FIXTURES / "snapshot.json").read_text(encoding="utf-8"))
    study = json.loads((FIXTURES / "study.json").read_text(encoding="utf-8"))
    return ro.OptimizerInputs.from_payload(
        portfolio_snapshot=payload["portfolio_snapshot"],
        validation_study=study,
        historical_rows=payload["historical_rows"],
        projected_pipeline_volume=payload["projected_pipeline_volume"],
        false_pass_budget_usd=payload["false_pass_budget_usd"],
        seed=0,
    )


def test_optimize_returns_feasible_parameters_within_bounds():
    inputs = _load_inputs()
    result = ro.optimize(inputs)
    assert result.schema_version == ro.RESERVE_OPTIMIZER_VERSION
    for domain, params in result.proposed.domains.items():
        cs_lo, cs_hi = ro._DEFAULT_BOUNDS["CS0_d"]
        al_lo, al_hi = ro._DEFAULT_BOUNDS["alpha_d"]
        assert cs_lo - 1e-9 <= params.CS0_d <= cs_hi + 1e-9, (
            f"{domain} CS0_d {params.CS0_d} out of bounds"
        )
        assert al_lo - 1e-9 <= params.alpha_d <= al_hi + 1e-9, (
            f"{domain} alpha_d {params.alpha_d} out of bounds"
        )


def test_optimize_improves_objective_versus_current_baseline():
    """The proposal must not be strictly worse than the current setting.

    The grid search seeds the candidate list with the current
    parameters, so the optimizer can fall back to the running policy
    when no grid point dominates it. On the synthetic frame the
    optimizer should find a better point in at least one domain.
    """

    inputs = _load_inputs()
    result = ro.optimize(inputs)
    assert result.delta.improved is True
    assert result.delta.objective_after <= result.delta.objective_before + 1e-9


def test_optimize_is_deterministic_on_fixed_seed():
    inputs = _load_inputs()
    a = ro.optimize(inputs)
    b = ro.optimize(inputs)
    assert a.to_canonical_bytes() == b.to_canonical_bytes()
    assert a.report_digest() == b.report_digest()


def test_optimize_changes_with_different_seed_does_not_break_determinism():
    """A different seed is allowed to change the inputs digest but the
    grid search itself is deterministic given identical row order, so
    the proposed parameter values should be stable.
    """

    inputs_seed_0 = _load_inputs()
    inputs_seed_1 = ro.OptimizerInputs(
        portfolio_snapshot=inputs_seed_0.portfolio_snapshot,
        validation_study=inputs_seed_0.validation_study,
        historical_rows=inputs_seed_0.historical_rows,
        projected_pipeline_volume=inputs_seed_0.projected_pipeline_volume,
        domains=inputs_seed_0.domains,
        false_pass_budget_usd=inputs_seed_0.false_pass_budget_usd,
        seed=1,
        grid_size=inputs_seed_0.grid_size,
        cost_overrides=inputs_seed_0.cost_overrides,
        prefer_scipy=False,
    )
    a = ro.optimize(inputs_seed_0)
    b = ro.optimize(inputs_seed_1)
    assert a.proposed.to_dict() == b.proposed.to_dict()
    # The canonical bytes differ because seed is recorded in the report.
    assert a.to_canonical_bytes() != b.to_canonical_bytes()


def test_cost_of_error_model_pulls_coefficients_from_validation_study():
    study = json.loads((FIXTURES / "study.json").read_text(encoding="utf-8"))
    model = ro.CostOfErrorModel.from_validation_study(study)
    assert model.beta_coherence == pytest.approx(1.8)
    assert model.beta_intercept == pytest.approx(-0.4)
    # Defaults retained when the report does not override them
    assert model.false_pass_cost_usd > 0.0
    assert model.lost_ev_usd > 0.0


def test_optimize_records_inputs_digest_and_method():
    inputs = _load_inputs()
    result = ro.optimize(inputs)
    assert isinstance(result.inputs_digest, str) and len(result.inputs_digest) == 64
    assert result.optimizer_method in {"grid", "scipy"}


def test_optimize_validates_negative_budget_rejects():
    inputs = _load_inputs()
    bad = ro.OptimizerInputs(
        portfolio_snapshot=inputs.portfolio_snapshot,
        validation_study=inputs.validation_study,
        historical_rows=inputs.historical_rows,
        projected_pipeline_volume=inputs.projected_pipeline_volume,
        domains=inputs.domains,
        false_pass_budget_usd=-1.0,
        seed=inputs.seed,
        grid_size=inputs.grid_size,
        cost_overrides=inputs.cost_overrides,
        prefer_scipy=False,
    )
    with pytest.raises(ro.ReserveOptimizerError):
        ro.optimize(bad)


def test_optimize_pipeline_cap_floor_when_volume_zero():
    inputs = _load_inputs()
    zero_vol = ro.OptimizerInputs(
        portfolio_snapshot=inputs.portfolio_snapshot,
        validation_study=inputs.validation_study,
        historical_rows=inputs.historical_rows,
        projected_pipeline_volume=0,
        domains=inputs.domains,
        false_pass_budget_usd=inputs.false_pass_budget_usd,
        seed=inputs.seed,
        grid_size=inputs.grid_size,
        cost_overrides=inputs.cost_overrides,
        prefer_scipy=False,
    )
    result = ro.optimize(zero_vol)
    assert result.proposed.pipeline_volume_cap >= ro._DEFAULT_PIPELINE_CAP


def test_optimize_reserve_target_within_reasonable_band():
    """The reserve target stays in [0, 0.20] of NAV by construction."""

    inputs = _load_inputs()
    result = ro.optimize(inputs)
    nav = float(inputs.portfolio_snapshot["fund_nav_usd"])
    assert 0.0 <= result.proposed.liquidity_reserve_fraction <= 0.20 + 1e-9
    assert 0.0 <= result.proposed.liquidity_reserve_target_usd <= nav * 0.20 + 1.0
