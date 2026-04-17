# Historical Backtest Pipeline — Spec

**Schema version:** `backtest-report-v1`
**Source module:** `server/fund/services/backtest.py`
**CLI verb:** `python -m coherence_engine backtest-run`
**Wrapper script:** `deploy/scripts/run_backtest.py`

## 1. Purpose

Replay the governed historical-outcomes dataset through the **current**
production scorer + decision policy with a **fixed** portfolio snapshot,
and emit per-row decision verdicts together with aggregate calibration
metrics. The output is a deterministic JSON report intended to feed
governance reviews and downstream calibration tooling.

## 2. Reproducibility contract

A backtest run is deterministic in the strong sense: given the same
`BacktestConfig` over the same input files, two invocations must
produce **byte-identical** report bytes.

This is enforced by:

* `BacktestReport.to_canonical_bytes()` uses
  `json.dumps(payload, sort_keys=True, separators=(",", ":"))` followed
  by a single `\n`.
* No wall-clock time, hostname, PID, or any other ambient environment
  is read by the report path.
* The portfolio state used by the decision policy comes exclusively
  from `BacktestConfig.portfolio_snapshot_path` — the live
  `portfolio_state` / `positions` tables are never queried.
* The configured `decision_policy_version` is asserted to match the
  running `DECISION_POLICY_VERSION` at the start of the run; mismatch
  raises `BacktestError` and the CLI exits with code `2`.

## 3. Inputs

### `BacktestConfig` (frozen dataclass)

| field                       | type                | meaning                                                                 |
|-----------------------------|---------------------|--------------------------------------------------------------------------|
| `dataset_path`              | `Path`              | governed-format JSONL or JSON array (see §3.1)                          |
| `decision_policy_version`   | `str`               | pin asserted against running `DECISION_POLICY_VERSION`                   |
| `portfolio_snapshot_path`   | `Path | None`       | JSON file describing a fixed `PortfolioSnapshot`; `None` = zero-default |
| `output_path`               | `Path | None`       | where to write the report; `None` = no file write                       |
| `seed`                      | `int`               | reserved for future stochastic extensions; recorded for audit           |
| `requested_check_usd`       | `float`             | per-row requested check size used for portfolio-gate evaluation         |
| `domain_default`            | `str`               | domain key used for rows that omit one (governed seed dataset does)     |

### 3.1 Dataset row schema

The dataset must validate via
`validate_historical_outcomes_export(...)`. Each row carries:

* `coherence_superiority` ∈ `[-1, 1]`
* `outcome_superiority` ∈ `[-1, 1]`
* `n_propositions` (int ≥ 1)
* `transcript_quality` ∈ `[0, 1]`
* `n_contradictions` (int ≥ 0)
* `layer_scores` (object with the five canonical keys: `contradiction`,
  `argumentation`, `embedding`, `compression`, `structural`).

Optional row-level overrides:

* `domain` — defaults to `BacktestConfig.domain_default`.
* `compliance_status` — defaults to `"clear"`.
* `anti_gaming_score` — defaults to `1.0` (clean).

Rows that fail normalization are counted in `n_skipped`; the validator
will fail the run before per-row replay if any row is *invalid* (as
opposed to merely incomplete after normalization).

### 3.2 Snapshot file schema

Permissive JSON object honoring any of:
`fund_nav_usd`, `liquidity_reserve_usd`, `drawdown_proxy`, `regime`,
`domain_invested_usd`. Unknown keys are ignored. The file is read-only.

## 4. Per-row replay

For each normalized row:

1. Compute the 95% calibrated superiority interval via
   `calibrated_superiority_interval_95(...)` using the row's
   `n_propositions`, `transcript_quality`, `n_contradictions`, and
   `layer_scores`.
2. Build a synthetic `application` (with `domain_primary`,
   `requested_check_usd`, `compliance_status`) and `score_record`
   (with `transcript_quality_score`, `anti_gaming_score`,
   `coherence_superiority_ci95={lower, upper}`).
3. Invoke `DecisionPolicyService().evaluate(application, score_record,
   portfolio_snapshot=snapshot)` — the snapshot is reused for every
   row (no live state).
4. Map row-level signals into Brier / reliability inputs (§5).

## 5. Metric definitions

* **Decision counts.** `pass_count`, `reject_count`,
  `manual_review_count` partition `n_rows`; their `_rate` siblings are
  the same divided by `n_rows`.
* **Predicted probability.** `predicted_probability =
  clamp((coherence_superiority + 1) / 2, 0, 1)`. A simple monotonic
  affine map; intentionally decoupled from the calibration profile so
  the backtest is portable across calibration-profile revisions.
* **Realized outcome (binary).** `realized_outcome = 1 if
  outcome_superiority > 0 else 0`.
* **Brier score.** `mean((predicted_probability -
  realized_outcome)^2)` across all replayed rows. Lower is better;
  bounded in `[0, 1]`.
* **Reliability curve.** Equal-width 10-bin histogram over
  `predicted_probability` ∈ `[0, 1]`. Each bin reports `count`,
  `mean_predicted`, `mean_realized`. The final bin is closed on the
  right so a probability of exactly 1.0 lands in bin 9. Empty bins
  carry zeros so the report shape is stable across datasets.
* **Mean predicted minus realized.** Signed delta between
  `mean_predicted_probability` and `realized_positive_rate`; positive
  values mean the system is **over-predicting** good outcomes.
* **Domain breakdown.** Same metrics restricted to rows whose
  `domain` matches; sorted by domain key for deterministic output.

## 6. Outputs

### 6.1 `BacktestReport` (frozen dataclass)

Top-level keys in the canonical JSON report (alphabetical):

```
{
  "aggregates":         { ...verdict counts/rates, brier, predicted/realized... },
  "config":             { ...echoed BacktestConfig (with resolved paths)... },
  "domain_breakdown":   { "<domain>": { ...metrics... }, ... },
  "generated_with":     {
    "decision_policy_version": "decision-policy-v1",
    "uncertainty_model_version": "fund-cs-superiority-v1"
  },
  "reliability_curve":  [ { "bin_index", "bin_lower", "bin_upper",
                            "count", "mean_predicted", "mean_realized" }, ...10 ],
  "rows":               [ { ...per-row replay record... }, ... ],
  "schema_version":     "backtest-report-v1"
}
```

### 6.2 Per-row record fields

`index`, `domain`, `coherence_superiority`, `ci_lower`, `ci_upper`,
`predicted_probability`, `realized_outcome`, `realized_superiority`,
`decision`, `threshold_required`, `margin`. All floats are rounded to
6 decimal places before serialization.

## 7. CLI

```
python -m coherence_engine backtest-run \
  --dataset    <path>      \
  --policy-version <pin>   \
  --portfolio-snapshot <path|omit>   \
  --output <path|omit>     \
  --seed <int>             \
  --requested-check-usd <float> \
  --domain-default <str>
```

Exit codes:

* `0` — backtest completed; canonical bytes written to stdout (and to
  `--output` when supplied).
* `2` — dataset validation failed, the policy-version pin did not
  match, or the snapshot file was unreadable. Error is on stderr; no
  partial report is produced.

## 8. Prohibitions / invariants

* **No live portfolio reads.** `BacktestConfig.portfolio_snapshot_path`
  is the sole source of portfolio state.
* **No network.** The service module imports nothing that performs
  network I/O.
* **No mutation of `data/governed/*`.** The backtest reads the dataset
  exclusively; tests assert that the source bytes are unchanged after
  a run.
* **Stable canonical serialization** (see §2). Reliability bins are
  emitted even when empty; per-row floats use a fixed 6-place rounding
  to avoid platform-dependent representations.
