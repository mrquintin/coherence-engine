# Reserve-allocation optimizer + decision-policy parameter proposals (prompt 70)

The reserve-allocation optimizer is the **proposal** half of the
quarterly parameter-tuning workflow for the decision policy. It is
**never** allowed to mutate the running policy by itself; all
production changes flow through an explicit operator-approval gate
documented below.

## What the optimizer does

Given:

* a **portfolio snapshot** (NAV, liquidity reserve, per-domain
  invested USD, regime, drawdown proxy),
* the most recent **validation study report** (prompt 44) -- in
  particular, the bootstrap point estimate of the logistic
  regression coefficient on `coherence_score`, which the optimizer
  uses as the slope of the cost-of-error model, and
* a **historical replay frame** (the prior 90 days of pitches with
  observed `coherence_superiority` and realized
  `outcome_superiority`) plus a **projected pipeline volume**,

the optimizer recomputes:

1. **Per-domain `(CS0_d, alpha_d)`** -- the intercept and slope of the
   coherence-gate threshold
   `cs_required = CS0_d + alpha_d * log2(S / S_min_d)`.
2. A **liquidity-reserve target**, expressed both as a fraction of NAV
   and as an absolute USD floor.
3. A **pipeline-volume cap** -- a soft throttle on open applications.

## Cost-of-error model

The optimizer's objective sums two error terms:

* **Lost EV**: a positive-outcome row that the proposed parameters
  *reject* contributes `lost_ev_usd * weight`.
* **False-pass cost**: a non-positive-outcome row that the proposed
  parameters *pass* contributes `false_pass_cost_usd * weight`.

Both are weighted by `max(1, check_size_usd / 50000)` so a $250k
write-down dominates a $50k one, mirroring the policy's existing
"single-check" sizing.

The default ratio (`false_pass_cost = 5x lost_ev`) is conservative;
operators MUST override the defaults with the calibrated estimates
from the validation study before promotion. The `beta_coherence` and
`beta_intercept` coefficients are pulled directly from the study's
`coefficients[]` block via `CostOfErrorModel.from_validation_study`.

## Constrained optimization

Formally the optimizer solves

    minimize  expected_lost_ev(theta)
    s.t.      expected_false_pass_exposure(theta) <= false_pass_budget_usd
              CS0_d in [CS0_min, CS0_max]
              alpha_d in [alpha_min, alpha_max]

When `scipy` is available **and** the operator passes
`--prefer-scipy`, the SLSQP path of `scipy.optimize.minimize`
(lazy-imported) is run. In every other case a deterministic 9x9 grid
search over `(CS0_d, alpha_d)` is used. The current parameters are
always added as an explicit candidate so the proposal is **never
strictly worse than the running policy** on the in-sample objective.
When no grid point satisfies the false-pass-budget constraint the
search relaxes the constraint and emits the unconstrained minimum;
the operator sees the relaxation in the report's `optimizer_method`
field.

## Backtest replay (the ex-ante check)

`reserve_optimizer.optimize` always runs the backtest replay on the
historical rows the caller supplied; it returns a populated
`BacktestDelta`:

* `pass_rate_before / pass_rate_after`
* `false_pass_exposure_usd_before / _after`
* `reserve_coverage_before / _after`
* `objective_before / _after`
* `improved` -- True iff
  `objective_after <= objective_before + 1e-9`.

The 90-day historical frame is the operator's responsibility (the
optimizer takes raw rows). The replay is required: the proposal blob
written to disk and to the database always carries the delta, so the
review CLI / committee can never see a proposal without seeing its
ex-ante effect.

## Proposal lifecycle

Every optimizer run is bound to a `PolicyParameterProposal` row:

```
proposed --(operator review)--> under_review --(admin approve)--> approved
                                              \\
                                               +--(admin reject)--> rejected
```

Constraints enforced by `PolicyParameterProposalService`:

* **Rate-limited**. A proposal whose `proposed.domains` overlaps a
  proposal less than `MIN_PROPOSAL_INTERVAL_DAYS` (30 days) old is
  refused with `PROPOSAL_RATE_LIMITED`.
* **Admin-only approval**. `approve` and `reject` require the
  principal to carry the `admin` role; `viewer` / `partner` get
  `PROPOSAL_FORBIDDEN` (the CLI maps this to a 403 spirit).
* **Explicit transitions**. Double-approving the same row, or
  approving a `rejected` row, raises
  `PROPOSAL_INVALID_TRANSITION`.

On approval the service emits a `policy_parameter_approved.v1`
outbox event (idempotency_key = `proposal:<id>:approved`). **The
event is informational only** -- no consumer in this prompt's SCOPE
auto-promotes the parameters into the running decision policy. The
operator runbook is responsible for the explicit promotion step.

## Decision frequency

Proposals run **at most monthly** per domain. The
30-day rate limit is the hard floor; the operating cadence is set by
the partner committee.

## Rollback plan

Each promoted parameter set is pinned by version under the
`PolicyParameterProposal` row. Rolling back a regrettable change is
as simple as **re-promoting the prior approved proposal**: the
operator runbook fetches the prior `approved` proposal id, the
runbook's promotion script reads the
`parameters_json["proposed"]` block, and writes a new policy version
with those values. No "undo" path exists in code -- rollback is
*explicitly a re-promotion* of an earlier proposal so the audit
trail captures the rollback as a regular approval transition.

## CLI surface

```
coherence-engine policy propose \
    --inputs portfolio_snapshot.json \
    --validation-study study.json \
    --output proposal.json \
    [--seed 0] [--proposed-by ops-eng] [--rationale "..."] [--prefer-scipy]
```

* Reads `portfolio_snapshot`, `validation_study`, `historical_rows`,
  `projected_pipeline_volume`, `false_pass_budget_usd` from `--inputs`.
* `--validation-study` overrides the study read from `--inputs`.
* Runs the optimizer + backtest replay, writes the canonical JSON
  to `--output`, and persists a `PolicyParameterProposal` row.
* Prints a one-line summary `{"proposal_id":..., "delta":{...}}`.

```
coherence-engine policy review --proposal-id N
```

Prints a structured diff: `per_domain` (CS0_d / alpha_d before vs.
after), `liquidity_reserve` (fraction + absolute USD), `pipeline_volume_cap`,
and `backtest` (the populated `BacktestDelta`).

```
coherence-engine policy approve --proposal-id N --principal-role admin
```

Marks the proposal `approved`, emits
`policy_parameter_approved.v1`. **Does NOT promote** the running
decision policy.

## Determinism

* Same `OptimizerInputs` (including the same seed) produces
  byte-identical `OptimizerResult.to_canonical_bytes()`.
* No wall-clock reads, no live database reads inside the optimizer.
* The `inputs_digest` is a SHA-256 over the serialized portfolio
  snapshot + validation study `data_hash` + row digest + budget +
  seed.

## Prohibitions (re-stated)

* Do **not** auto-promote optimizer output. Approval is explicit.
* Do **not** propose parameters more than once a month per domain
  (rate-limit at the CLI / service layer).
* Do **not** skip the backtest replay; it is the ex-ante check.
* Do **not** wire the optimizer to the running decision-policy
  module's `_params_for_domain` -- it simulates the coherence-gate
  locally so a mis-fit proposal cannot leak into production via an
  import-time side effect.
