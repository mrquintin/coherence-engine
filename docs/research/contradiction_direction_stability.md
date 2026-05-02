# Contradiction direction (ĉ) — cross-domain stability study

**Prompt 48 (Wave 13).** This document describes the design,
falsification thresholds, and operating procedure for the cross-domain
stability study of the contradiction-direction vector ĉ derived in
`apps/site/src/content/research/contradiction_direction.mdx`.

The original ĉ paper showed that, given a contradiction pair (u, v),
the Householder reflection axis n = (u − v) / ‖u − v‖ surfaces the
contested semantic axis. The aggregate contradiction direction ĉ for
a *set* of pairs is the leading principal component of the per-pair
{n_i}. The open question is whether **a single ĉ vector
generalises across discourse domains**, or whether each domain (legal,
fiction, telephony, …) carries its own contradiction geometry that
demands a per-domain ĉ. This study tests that.

This is an *adversarial* internal study: its purpose is to recommend
**either** a single global ĉ for the engine **or** a per-domain
collection of ĉ vectors, and to publish whichever the data supports.
A negative result (per-domain ĉ wins) is the ship-blocking finding
that would force the engine to carry one fitted vector per domain.

## What gets measured

| Concept                     | Operationalisation                                                          |
|-----------------------------|------------------------------------------------------------------------------|
| Domain                      | MultiNLI ``genre`` field (fiction, government, slate, telephone, travel)     |
| Per-domain ĉ                | ``core.contradiction_direction.fit_c_hat`` on that domain's contradiction pairs |
| ĉ-similarity metric         | abs-cosine — sign-invariant because ĉ is only defined up to sign             |
| Cross-domain discriminator  | ``project(pairs, ĉ)`` = ``|⟨u − v, ĉ⟩|``                                     |
| Cross-domain ROC AUC        | rank-sum AUC for contradiction (positive) vs entailment (negative)           |
| Within-domain baseline AUC  | 50 / 50 deterministic split — fit on first half, evaluate on second          |
| Subsample sensitivity       | ĉ refit on N ∈ {200, 500, 1000} subsets of the pooled contradictions × 50 seeds |
| Confidence intervals        | 95 % bootstrap (n = 1000, stdlib indices into numpy math)                    |
| Pre-registration            | ``Experiments/Contradiction_Direction_Stability/preregistration.yaml`` v1.0  |

## Falsification criterion (decision rule)

> A **single ĉ generalises** across domains when **both** of these hold:
>
> 1. The minimum pairwise abs-cosine across all domain ĉ vectors is **≥ 0.70**.
> 2. The median cross-domain AUC drop relative to the within-domain
>    baseline is **≤ 0.05**.
>
> Otherwise — either condition fails — the engine **requires per-domain ĉ**.

The `0.70` cosine threshold is the conventional "strong directional
agreement" boundary; below 0.70 the axes describe meaningfully
different contradictions. The `0.05` AUC drop threshold is the
just-noticeable-difference for a balanced binary discriminator; a
larger drop means a per-domain ĉ would do measurably better. Both
thresholds are pinned in code as
`_FALSIFICATION_PAIRWISE_COSINE = 0.70` and
`_FALSIFICATION_AUC_DROP = 0.05` in
`Experiments/Contradiction_Direction_Stability/run_stability_study.py`,
and any change to either requires a new `version:` in
`preregistration.yaml` with a written `justification:` stanza.

The harness emits the same canonical-JSON report shape regardless of
which way the decision lands; no result path is short-circuited on the
basis of which outcome it produces.

## Cross-fit protocol (no leakage)

> ĉ fit on domain A is evaluated **only** on held-out pairs from
> domain B (B ≠ A).

Within-domain AUC is computed by a deterministic 50 / 50 split: ĉ is
fit on the first half of A's contradiction pairs and evaluated on the
second. This baseline is what cross-domain AUC is compared against —
*not* an in-sample fit, which would be optimistically biased.

Reporting an in-sample AUC (training pairs reused for evaluation) is
explicitly listed in `prereg.prohibited_actions`.

## Determinism contract

The harness mirrors the cosine-paradox replication's contract:

* Same `StabilityConfig` + same input pairs → byte-identical
  `to_canonical_bytes()` output (after stripping the volatile
  `generated_with` block that records detected library versions).
* All bootstrap and subsample index sampling is driven by
  `random.Random` seeded from `preregistration.seeds.*`. Numpy is used
  only for SVD and matrix arithmetic; no calls into `numpy.random`
  occur.
* The pinned `STABILITY_SCHEMA_VERSION = "c-hat-stability-v1"` flips
  on any change to the report schema.

## Operating procedure

```
# Smoke test: run on the bundled tiny synthetic fixture (≈2 s).
python -m coherence_engine replication c-hat-stability --dry-run

# Real run: pre-embedded per-domain pair corpus (no network access).
python -m coherence_engine replication c-hat-stability \
    --corpus path/to/corpus.json \
    --output reports/c_hat_stability.json
```

The corpus JSON shape is documented in `load_pair_corpus`:

```json
{
  "schema": "c-hat-stability-fixture-v1",
  "model_id": "sentence-transformers/all-mpnet-base-v2",
  "model_version": "1.0.0",
  "dim": 768,
  "domains": {
    "fiction":   {"contradiction": [...], "entailment": [...]},
    "government":{"contradiction": [...], "entailment": [...]},
    ...
  }
}
```

Each pair is a `[u, v]` of `dim`-length float vectors. The harness
refuses to emit a report when any domain has fewer than
`stopping_rule.minimum_pairs_per_domain_label` pairs (default 200).

## Synthetic-fixture sanity check

The bundled fixture is a 2-domain synthetic corpus in dim = 4 with
intentionally orthogonal contradiction axes:

* `domain_a` contradictions are aligned with axis e₀.
* `domain_b` contradictions are aligned with axis e₁.

End-to-end, the harness recovers ĉ_a ≈ e₀, ĉ_b ≈ e₁, pairwise
abs-cosine ≈ 0.0, within-domain AUC ≈ 1.0, and cross-domain AUC far
from baseline. The decision rule correctly outputs
`per_domain_c_hat_required`. This is the test fixture asserted in
`tests/test_c_hat_stability.py` and is the floor on what the harness
must catch — if the fixture ever flips to `single_c_hat_generalises`,
the harness is broken before any real data is in scope.

## Interpreting the report

The canonical report has five top-level analytical blocks:

1. **`per_domain_c_hat`** — the fitted ĉ for each domain plus
   metadata. The literal vector is included so downstream consumers
   can use it directly without re-fitting.
2. **`pairwise_cosine`** — the full N × N abs-cosine matrix between
   per-domain ĉ vectors with bootstrap CIs, plus min / max / mean /
   median across the off-diagonal.
3. **`cross_domain_auc`** — within-domain baseline AUCs and the full
   cross-domain matrix, each entry annotated with
   `auc_drop_vs_baseline = within_baseline(B) − cross(A→B)`. Negative
   drops are possible (rare) and indicate ĉ_A actually outperforms
   ĉ_B on B's pairs.
4. **`subsample_sensitivity`** — for each pre-registered N,
   `n_subsamples` random subsets of the pooled contradiction set are
   used to refit ĉ and report mean / std / 95 % CI of the abs-cosine
   to ĉ(full pool). Convergence is the ratio of these means as N
   grows.
5. **`decision`** — the criterion text, the two thresholds, the two
   observed quantities, the boolean `single_c_hat_holds`, and the
   string `outcome ∈ {single_c_hat_generalises, per_domain_c_hat_required}`.

A reader who only looks at `decision.outcome` should still get the
right answer; the upstream blocks exist so a reviewer can audit the
chain.

## Limits

* MultiNLI's `genre` field is a coarse domain proxy. A finer-grained
  domain split (legal, medical, source-code commentary) would
  strengthen the conclusion in either direction. We deliberately do
  not stratify by topic *within* a genre because that conflates
  domain with content.
* The cross-domain AUC depends on the ratio of contradiction to
  entailment pairs in B; a domain with very few entailment pairs gets
  a noisy baseline. The stopping rule (≥ 200 per label) is the
  conservative defence against this.
* ĉ being only defined up to sign is handled with abs-cosine and
  abs-projection. A consumer that uses ĉ in a sign-sensitive way
  (e.g., to *direct* a downstream rotation rather than just measure
  alignment) needs an additional sign-fix step appropriate to its
  domain.
* The bundled fixture is dimension 4 (synthetic). The real run uses
  dimension 768 from the same encoder used in the cosine-paradox
  replication, so the two studies share one encoder failure mode.

## Pointers

* `core/contradiction_direction.py` — `fit_c_hat`, `project`,
  `abs_cosine`, `pair_directions`.
* `Experiments/Contradiction_Direction_Stability/run_stability_study.py`
  — harness entrypoint and CLI helpers.
* `Experiments/Contradiction_Direction_Stability/preregistration.yaml`
  — frozen pre-registration document.
* `tests/test_c_hat_stability.py` — geometry, determinism,
  decision-rule, and CLI smoke tests.
