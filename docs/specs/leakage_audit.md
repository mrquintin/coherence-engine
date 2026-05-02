# Leakage audit + temporal pre/post-2020 holdout split — spec

Status: **load-bearing**, prompt 45 (Wave 12).

The leakage audit is the gate that runs *before* any validation-study
report can be rendered. It exists to make a single guarantee falsifiable:

> No pitch in the post-2020 holdout was used, directly or transitively,
> to fit any artifact whose output the study consumes.

The audit raises `LEAKAGE_DETECTED` (`server/fund/services/leakage_audit
.py::LeakageDetectedError`) on any failed assertion. The validation
study harness (`run_study`) catches no part of this: a failing audit is
an exception that propagates out, no canonical bytes are written, and
the CLI returns a non-zero exit code.

## Why a buffer year

The training corpus for the contradiction-direction vector ĉ, the
anti-gaming templates, and the calibration curves was last refit in
late 2020. Because outcome labels (5-yr survival, exit event) lag the
pitch by several years, *any* pitch dated within the same calendar
window in which a fit was performed risks bidirectional contamination:
the pitch could have been observed during fit selection, *and* the
artifacts could shape its scoring before its outcome was sealed.

The audit's defense is structural rather than statistical: a full
calendar year (the **buffer year**) is excluded from both the training
window and the holdout window. The defaults are pinned in
`server/fund/services/temporal_split.py`:

```
train_end       = "2019-12-31"   # inclusive upper edge of training
buffer_year     = 2020           # excluded from both partitions
holdout_start   = "2021-01-01"   # inclusive lower edge of holdout
```

Pitches dated 2020 are placed in `SplitResult.buffer_excluded` and
never reach either side of the audit. Shrinking the buffer year
requires (a) an explicit `--buffer-year` override on the CLI plus
`--buffer-override-rationale`, and (b) a corresponding amendment in
`data/governed/validation/preregistration.yaml` per the prompt 45
prohibitions. The audit emits a `failed_assertion` whenever
`buffer_year != 2020` is requested without a written rationale.

## Audit assertions

### 1. Training-set membership (`artifact_membership`)

For each artifact listed in
`data/governed/training_artifacts_index.json`, the audit:

* Looks up the `training_pitch_ids` that produced the artifact.
* Computes `holdout ∩ training_pitch_ids` per artifact.
* Raises a failed assertion when the intersection is non-empty.

The training-artifacts index is operator-maintained: whenever the
contradiction-direction vector ĉ, the anti-gaming template set, or any
calibration curve is recomputed, the operator updates the artifact's
`training_pitch_ids`, `training_set_hash`, `fit_at`, and `fit_by`
fields. CI verifies that the recorded hash matches a recomputed hash
over the sorted `training_pitch_ids` list, and the audit emits a
`warning` when the two diverge.

### 2. Temporal-window integrity (`temporal_split`)

The audit runs `temporal_split.split` over the corpus and:

* Records `n_train`, `n_holdout`, `n_buffer_excluded`, `n_undated_excluded`.
* When the caller passes an explicit `holdout_pitch_ids` set, the
  audit asserts that every declared id falls inside the post-buffer
  window. Any id outside the window is recorded in
  `holdout_outside_window_pitch_ids` and raises a failed assertion.

### 3. Distribution drift (`feature_drift`)

For every `feature_extractors` field name supplied by the caller, the
audit computes:

* The two-sample Kolmogorov–Smirnov statistic between the training and
  holdout marginals, with the standard `1.36 / sqrt((n1+n2)/(n1*n2))`
  critical value at α=0.05. A KS alarm becomes a `warning`.
* The Population Stability Index using quantile bins derived from the
  *training* distribution. Thresholds:
  * `PSI < 0.25` → `ok`
  * `0.25 ≤ PSI < 0.50` → `warn`
  * `PSI ≥ 0.50` → **error**

A PSI in the error band raises a failed assertion under
`distribution_drift`. The justification is operational, not theoretical:
a holdout whose marginals are wildly different from the training set is,
downstream, indistinguishable from a leak that smuggled the wrong
population into the holdout. We refuse the render either way.

## Wiring

```
                +------------------+
                | StudyConfig      |
                +--------+---------+
                         |
                         v
              +----------+----------+
              | run_study           |
              | (validation_study)  |
              +----------+----------+
                         |
              loads corpus_manifest_path
                         |
                         v
                   +-----+-----+
                   | leakage_  |
                   | audit.audit|
                   +-----+-----+
                         |
        +----------------+-----------------+
        |                |                 |
        v                v                 v
  membership      temporal split     KS + PSI
        \                |                 /
         \               v                /
          +---> LeakageReport <----------+
                         |
                         v
                 enforce(report)
                         |
                  passed?  no  ----> raise LeakageDetectedError
                         | yes
                         v
                  write canonical bytes
```

`run_study` calls `leakage_audit.audit` *after* fitting and metric
computation but *before* writing the canonical bytes. A failed audit
raises before any output is produced, so a half-written report is not
a possible end state.

## Operator runbook

```bash
# Audit the on-disk corpus (uses default training-artifacts index).
python -m coherence_engine leakage audit

# Include a drift check on a feature column.
python -m coherence_engine leakage audit --feature pitch_year

# Use a non-default buffer year. Requires a written rationale.
python -m coherence_engine leakage audit \
    --buffer-year 2021 \
    --buffer-override-rationale "see preregistration v1.1 amendment"
```

Exit codes: `0` = passed, `2` = `LEAKAGE_DETECTED` (or
`TrainingArtifactsIndexError`).

When the audit passes, the JSON report is written to stdout (and
optionally to `--output PATH`). The report is canonical:

```json
{
  "schema_version": "leakage-audit-report-v1",
  "passed": true,
  "audit_digest": "...sha256...",
  "config": {...},
  "temporal_split": {...},
  "artifact_membership": [...],
  "feature_drift": [...],
  "failed_assertions": [],
  "warnings": []
}
```

## Determinism contract

* Same `corpus`, same training-artifacts index, same feature
  extractors → identical `audit_digest`.
* No wall-clock reads, no network calls, no random sampling.
* Pure stdlib (no numpy / scipy) — the project's baseline-environment
  guarantee from prompt 44 still holds.

## Prohibitions (prompt 45)

* The leakage audit MUST run on every `validation-study run` invocation.
  The block is not configurable.
* The buffer year MUST be 2020 unless the operator explicitly overrides
  it AND records a written rationale in the preregistration YAML.
* The holdout set MUST NOT be derived from any fitted artifact's
  training pool.
