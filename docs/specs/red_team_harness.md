# Red-team adversarial harness

**Status:** Implemented (prompt 13 of 20, Wave 5).
**Owner:** Fund Backend.
**Code:** `server/fund/services/red_team.py`,
`tests/test_red_team_harness.py`, `tests/adversarial/`,
CLI: `red-team-run`.

## What this is

A curated, deterministic, fully-offline regression harness that
replays a labeled corpus of adversarial founder pitches through the
production scoring + decision pipeline (`ScoringService` →
`DecisionPolicyService`, the same chain used by
`ApplicationService.process_next_scoring_job`) and reports per-case
results, false-pass / false-reject / false-review counters, and a
3 × 3 confusion matrix against ground-truth labels.

The harness has two distinct consumers, each with its own success
criterion:

1. **CI / `pytest`** — pins the confusion matrix as a regression
   baseline. Drift fails loudly with a diff so reviewers can
   distinguish intentional behavior changes from accidental
   regressions.
2. **Operators / `red-team-run` CLI** — emits a deterministic JSON
   report and signals via exit code whether every fixture's actual
   verdict matched its labeled expectation.

## Verdict vocabulary

The harness uses the canonical external verdict set:

| verdict         | meaning                                                  |
| --------------- | -------------------------------------------------------- |
| `pass`          | clean approval, founder is notified                      |
| `reject`        | hard reject (decision policy returns `fail` internally)  |
| `manual_review` | warning band, partner escalation packet recommended      |

The decision policy emits `fail` internally for hard-reject
outcomes; the harness translates `fail` → `reject` to match the
canonicalization in `ApplicationService` and the
`decision_issued.v1` event schema.

## Fixture authoring guide

Every fixture is a single JSON file in
`tests/adversarial/fixtures/` with these required fields:

| field                   | type    | notes                                          |
| ----------------------- | ------- | ---------------------------------------------- |
| `id`                    | string  | unique short slug, used in the report         |
| `category`              | string  | one of `incoherent`, `coherent_evidenced`, `template_echo`, `borderline` |
| `one_liner`             | string  | the founder's elevator pitch                  |
| `use_of_funds_summary`  | string  | requested ask narrative                       |
| `requested_check_usd`   | number  | numeric USD                                   |
| `domain_primary`        | string  | `market_economics`, `public_health`, `governance` |
| `compliance_status`     | string  | `clear`, `review_required`, `blocked`         |
| `transcript_text`       | string  | the synthesized "transcript" body the scorer sees |

A fixture is added to the suite by:

1. Drop the JSON into `tests/adversarial/fixtures/<id>.json`.
2. Add a matching entry to `tests/adversarial/labels.json`:
   ```json
   {
     "<id>.json": {
       "expected_verdict": "pass | reject | manual_review",
       "rationale": "one-sentence justification"
     }
   }
   ```
3. Re-run the harness and update the pinned confusion matrix in
   `tests/test_red_team_harness.py` (see "Bumping the pin" below).

### Category guidance

Pick the smallest category that fits, then write the smallest
fixture that exercises it. Long fixtures dilute signal.

* **`incoherent`** — multiple intra-sentence contradictions, hard
  compliance blocks, or zero quantified evidence. Expected
  `reject`.
* **`coherent_evidenced`** — concrete metrics (revenue, NRR,
  churn, pilot data), bounded TAM, named team, and a clear
  use-of-funds. Expected `pass`.
* **`template_echo`** — pure VC-pitch template language with no
  real numbers ("leading platform", "trillion-dollar TAM",
  AI-native repetition). Expected `manual_review`.
* **`borderline`** — real but thin evidence, or
  `compliance_status="review_required"`. Expected `manual_review`.

### Prohibitions for fixtures (prompt 13)

* **No real founders or companies.** All names must be
  synthetic; favor neutral words ("Meridian Analytics",
  "Orchid Care", "Civic Beacon"). Do not encode initials,
  ticker symbols, or recognizable internal codenames.
* **No PII, no real geographic identifiers.** Synthetic
  municipalities are fine; named individuals are not.
* **Fully offline.** Fixtures must not reference URLs that
  would be fetched, model artifacts that would be downloaded,
  or governed datasets that the harness cannot resolve from
  the repo alone.

## Per-case output schema

Each case in the emitted report has:

```jsonc
{
  "fixture_id": "coh_001_saas_metrics",
  "fixture_filename": "coh_001_saas_metrics.json",
  "category": "coherent_evidenced",
  "expected_verdict": "pass",
  "actual_verdict": "reject",
  "matches_label": false,
  "coherence_superiority": -0.019644,
  "coherence_superiority_ci95": {"lower": -0.096698, "upper": 0.05741},
  "anti_gaming_score": 1.0,
  "anti_gaming_flags": [],
  "transcript_quality_score": 1.0,
  "failed_gate_codes": ["ANTI_GAMING_HIGH", "COHERENCE_BELOW_THRESHOLD"],
  "threshold_required": 0.760482,
  "coherence_observed": -0.096698,
  "margin": -0.85718,
  "rationale": "..."  // mirrored from labels.json
}
```

The aggregate report wraps these with `total_cases`, `matches`,
`mismatches`, `counts` (`false_pass`/`false_reject`/`false_review`),
and the `confusion_matrix`.

## CLI

```
python -m coherence_engine red-team-run \
  --fixtures-dir tests/adversarial/fixtures \
  --labels tests/adversarial/labels.json \
  --output /tmp/ce-redteam.json
```

Optional `--policy-version` pins the decision-policy version (must
match the running `DECISION_POLICY_VERSION`; mismatch is exit 2).

| exit | meaning                                                                 |
| ---- | ----------------------------------------------------------------------- |
| 0    | every fixture's actual verdict matched its labeled `expected_verdict`. |
| 1    | at least one mismatch (false-pass / false-reject / false-review).      |
| 2    | fixture / labels could not be loaded, or policy-version pin mismatch.  |

The report is always written to stdout; passing `--output` mirrors
it to disk.

## Pinned confusion matrix

The CI test
`tests/test_red_team_harness.py::test_confusion_matrix_matches_pinned_baseline`
asserts the harness produces a specific confusion matrix on the
12-fixture corpus. This is a **drift detector**, not an assertion
of "correct" behavior. The pin captures the current behavior of
the production scoring + decision pipeline so any change to:

* the `CoherenceScorer` weights, layers, or anti-gaming detector,
* the `DecisionPolicyService` thresholds or gate set,
* the calibrated uncertainty interval,
* the canonical-verdict translation,

immediately surfaces as a test failure with a clear diff.

### Bumping the pin

When a behavior change is intentional:

1. Run the harness locally:

   ```bash
   python -m coherence_engine red-team-run \
     --fixtures-dir coherence_engine/tests/adversarial/fixtures \
     --labels coherence_engine/tests/adversarial/labels.json \
     --output /tmp/ce-redteam.json
   ```

2. Inspect the new `confusion_matrix` and `counts` in the report.
3. Update `EXPECTED_CONFUSION_MATRIX` and `EXPECTED_TOTALS` in
   `tests/test_red_team_harness.py` to match.
4. In the same PR, document **why** the behavior changed:
   * which prompt or refactor caused the shift,
   * which fixtures moved between cells,
   * whether the shift is improvement, regression, or neutral.
5. Cross-link the PR in this section if the shift establishes a
   new guarantee (e.g. "anti-gaming convention inversion fixed").

A bump that lacks a PR-level justification should be rejected on
review.

## Determinism guarantees

* `ScoringService` is constructed with the local `tfidf` embedder
  and `heuristic` contradiction backend (no remote model
  downloads, no GPU).
* `RedTeamReport.to_canonical_bytes()` sorts keys and uses fixed
  separators; consecutive runs are byte-identical (asserted by
  `test_report_is_byte_deterministic_across_runs`).
* Fixtures are iterated in `sorted(filename)` order regardless of
  OS directory iteration order.
* No DB writes, no outbox events, no portfolio-state reads.

## Future work

* Add per-domain confusion matrices when the corpus grows beyond
  ~40 fixtures.
* Wire the harness to dispatch a `red_team_completed` event so
  downstream analytics can track drift over time.
* Once the anti-gaming convention is unified across the scoring
  service and the decision policy, regenerate the pin and document
  the shift in this section.
