# Validation study spec — coherence vs. outcome regression

**Prompt 44, Wave 12.** Pairs with `historical_corpus.md` (prompt 42) and
`outcome_labeling.md` (prompt 43). Implementation lives in
`server/fund/services/validation_study.py`; pre-registration text at
`data/governed/validation/preregistration.yaml`.

## What this study answers

> Does the production coherence_score carry non-trivial **predictive**
> information about realized 5-year survival, after controlling for the
> domain a pitch belongs to and the check size that was on the table?

The study is a screening question, not a causal one. A positive
association does **not** establish that a higher coherence score
*causes* better survival — only that the score is a useful filter. The
spec calls this out in the report under a `disclosure` section so a
reader cannot mistake the claim being made.

## Pre-registration

The full document is at `data/governed/validation/preregistration.yaml`.
Highlights:

- **Primary hypothesis (H1):** coherence_score is positively associated
  with survival_5yr at α = 0.01 (one-sided), in a logit of
  `survival_5yr ~ coherence_score + domain_primary + log(check_size_usd)`.
- **Secondary hypothesis (H2):** monotonic non-decreasing dose-response
  across coherence quintiles, with Q5 - Q1 ≥ 0.05.
- **Stopping rule:** the study runs *only* when the joined frame has
  N ≥ 200 rows with a known (non-`unknown`) survival_5yr label. Below
  that the runner raises `INSUFFICIENT_SAMPLE` and emits no report.
- **Multiple-comparison correction:** Bonferroni across the per-domain
  sub-models. Each sub-model's α is the family α (0.05 by default)
  divided by the number of sub-models that passed the per-domain
  minimum N.
- **Bootstrap:** n = 10000 row-level resamples by default, deterministic
  RNG seeded by `config.seed`.

### Pre-registration as a contract

The runner refuses to operate on a preregistration that is missing
required keys, and `version:` plus an `amendments:` stanza is the only
way to bump any pinned constant. Editing the document silently after
seeing the data is the canonical failure mode this whole layer exists
to prevent.

## What the report contains

`StudyReport.to_canonical_bytes()` produces a sort-keyed JSON document
with these top-level fields:

| Field | Description |
|---|---|
| `schema_version` | `validation-study-report-v1` |
| `config` | resolved input paths, seed, bootstrap iterations |
| `preregistration` | full pre-registration document, embedded |
| `n_total` | rows in the joined corpus + outcomes frame (incl. unknown) |
| `n_known_outcome` | rows with a known survival label, used in the model |
| `n_excluded_unknown` | rows dropped because survival was 'unknown' |
| `coefficients` | every model term with point estimate + 95 / 99 % bootstrap CIs |
| `primary_hypothesis_result` | H1 pass/fail with the CI used to decide |
| `secondary_hypothesis_result` | H2 quintile dose-response table |
| `metrics` | AUC (ROC), Brier score, mean predicted probability, realized rate |
| `calibration_curve` | 10-bin reliability table (mean predicted vs. mean realized) |
| `domain_breakdown` | per-domain sub-models with Bonferroni-corrected CIs |
| `insufficient_subgroups` | domains below the per-domain minimum N |
| `data_hash` | SHA-256 of the joined frame, for provenance |

## Determinism

`run_study` is byte-deterministic for a fixed `(StudyConfig, frame)`:

* All math is pure stdlib (Newton–Raphson IRLS for the logit, Cholesky
  for the Newton step, custom rank-based AUC, Mann–Whitney style).
* Bootstrap resampling uses `random.Random(config.seed)`. No global
  RNG; no `time.time()` calls anywhere in the path.
* `numpy` / `statsmodels` / `sklearn` are *opportunistically* imported
  via `_detect_optional_libs()` and recorded in `generated_with` for
  audit; nothing else in the harness consults them.
* The report dict is round-tripped through `json.dumps(..., sort_keys=True)`
  before being returned, so callers cannot corrupt internal state by
  mutating `report.config` and the returned bytes are stable across
  Python implementations.

## Negative results

The pre-registration explicitly says:

```
negative_results_policy:
  publish_when_null: true
  publish_when_wrong_sign: true
```

A null or negative-direction result emits the same report shape as a
positive one — it is an answer, not a failure. We commit to publishing
either way. The harness even includes the bootstrap CI on
**counter-direction** evidence (the upper 99% bound, when the lower is
clearly below zero) so a wrong-sign finding is immediately visible.

## Boundary: prediction, not causation

```
scope_boundary:
  claim_kind: "prediction"
  not_claim_kind: "causation"
```

If `coherence_score` improves AUC after controlling for domain and
check size, the score is a useful screening signal — and that is the
only claim the study is licensed to make. Causal claims would require a
randomized trial (which we are not doing), proper instruments, or at
minimum a structural causal model (which the corpus does not support).
Any operator citing the study should reproduce that boundary.

## Operator runbooks

### Run the study

```
python -m coherence_engine validation-study run \
    --output reports/study.json \
    --scores reports/coherence_scores.json \
    --seed 0
```

`--scores` is a JSON object of shape:

```json
{
  "<pitch_id>": {"coherence_score": 0.82, "check_size_usd": 250000},
  ...
}
```

Pitches missing from this file are dropped from the frame (and counted
in the report's "n_total - n_known_outcome - n_excluded_unknown" gap).
This split is intentional: the score artifact is the *audited* record
of what the production scorer produced, and the join is the place where
"this pitch was scored" / "this pitch had an outcome" become a single
row.

The same entry point is also available via
`deploy/scripts/run_validation_study.py` for ops contexts that don't
have an editable install of the CLI package.

### Render a Markdown brief

```
python -m coherence_engine validation-study report --in reports/study.json
```

Emits a human-readable Markdown report that mentions: N(known outcome),
the primary-hypothesis decision, AUC, Brier, the calibration curve, the
coefficient table, per-domain sub-models, and the disclosure section.
The renderer is a pure function of the report payload, so two reports
with the same digest yield byte-identical Markdown.

### Amending the pre-registration

```
amendments:
  - version: "v1.1"
    on: "2026-05-01"
    by: "michael@..."
    rationale: "..."
    changed_fields: ["primary_hypothesis.alpha"]
```

Bumping `version:` is mandatory; any operator who edits the YAML
without that audit trail is flagged by the next CI run that verifies
`version` and the `amendments` log are coherent.

## Glossary

* **Brier score:** mean squared error between predicted probability and
  realized 0/1 outcome. Lower is better; 0 is perfect, 0.25 is the
  always-0.5 baseline, 1 is maximally wrong.
* **AUC (ROC):** area under the receiver operating characteristic. The
  probability that a randomly chosen positive outcome ranks higher
  than a randomly chosen negative one. 0.5 = no signal, 1.0 = perfect.
* **Calibration curve:** for each predicted-probability bin, the mean
  predicted probability vs. the mean realized outcome. A 45° line
  means the model is calibrated; deviations above the diagonal mean
  the model is over-confident in the negative direction.
