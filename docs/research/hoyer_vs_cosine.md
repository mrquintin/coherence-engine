# Hoyer-sparsity vs raw-cosine — head-to-head ROC harness

**Prompt 49 (Wave 13).** This document describes the design, the
falsification criterion, and the operating procedure for the head-
to-head ROC comparison of three discriminators of NLI contradiction
vs entailment:

  * `cosine`     — `1 - cos(u, v)` (higher score => more contradiction)
  * `hoyer`      — `hoyer_sparsity(u - v)` of the difference vector
  * `projection` — `|⟨u - v, ĉ⟩|` against the contradiction direction
                   ĉ fit on a held-out half of the contradiction pairs

The Guide-3 claim under test is that **the engine's signal is
*meaningfully different* from raw cosine**. This harness either
supports that claim, refutes it (cosine wins or there is no
difference), or returns a null. All three outcomes are publishable
under the pre-registered negative-results policy.

This is a *comparison* study, not a replication. Cosine is a single
fixed baseline; Hoyer and projection are the engine's claimed
upgrades. The DeLong test asks whether the AUC difference between
each pair of classifiers is statistically distinguishable from zero
at the pre-registered alpha.

## What gets measured

| Concept                | Measurement                                                   |
|------------------------|---------------------------------------------------------------|
| Discriminator scores   | `cosine`, `hoyer`, `projection` (defined above)               |
| Per-classifier point   | ROC AUC via the rank-sum formula with average-rank tie correction |
| Per-classifier CI      | 95 % paired bootstrap (one resampled index set, three AUCs)   |
| Pairwise comparison    | DeLong two-sided z-test for AUC equality                      |
| Bootstrap iterations   | 10 000 (pinned in `preregistration.yaml`)                     |
| α (DeLong)             | 0.01 (pinned)                                                 |
| Stopping rule          | ≥ 200 eval pairs per label after the 50/50 fit/eval split     |
| Negative-results policy | Publish in any direction (Hoyer wins, cosine wins, or null)  |

## Falsification criterion

> The Guide-3 claim is **supported** when the DeLong two-sided z-test
> rejects the null AUC(`hoyer`) == AUC(`cosine`) at α = 0.01. The
> *direction* — which classifier wins — is read off the sign of
> `auc_diff` and reported.
>
> The claim is **refuted** when the test fails to reject (i.e. there
> is no evidence that Hoyer's signal differs from cosine's), OR when
> the test rejects but in the wrong direction (cosine wins).

The two-sided test is load-bearing: a one-sided test would let a
result in the unanticipated direction (cosine winning) slip past the
gate, undermining the negative-results policy. The pre-registration
explicitly prohibits a post-hoc switch to a one-sided test.

## DeLong's test — why this and not an unpaired comparison

All three classifiers score the *same* eval pairs, so their AUC
estimates are correlated. DeLong's algorithm exploits that pairing
explicitly:

  1. For each classifier `r` and the eval pairs partitioned into
     positive `X[r]` (length `n_pos`) and negative `Y[r]` (length
     `n_neg`), compute the placement values

         psi(a, b) = 1 if a > b, 0.5 if a == b, 0 otherwise
         V10[r][i] = mean_j psi(X[r][i], Y[r][j])
         V01[r][j] = mean_i psi(X[r][i], Y[r][j])
         AUC[r]    = mean(V10[r]) = mean(V01[r])

  2. Compute the sample covariance matrices `S10[r,s]` (over the
     `n_pos` eval positives) and `S01[r,s]` (over the `n_neg` eval
     negatives). The combined covariance is

         S = S10 / n_pos + S01 / n_neg

  3. For the paired test of AUC[r] == AUC[s]

         z = (AUC[r] - AUC[s]) / sqrt(S[r,r] + S[s,s] - 2*S[r,s])
         p_two_sided = 2 * (1 - Φ(|z|))

The off-diagonal covariance term `S[r,s]` is exactly what an
unpaired test (e.g. independent two-sample z) discards. Discarding
it would inflate the variance estimator and rob the test of power
that the paired evaluation actually has.

## Cross-fit protocol — no in-sample evaluation of ĉ

ĉ-projection requires a fit. To prevent leakage, the harness
deterministically splits both labels 50/50 using
`random.Random(seed)`:

  * `pos_fit`, `pos_eval` — contradiction pairs
  * `neg_fit`, `neg_eval` — entailment pairs

ĉ is fit on `pos_fit` only. All three scores (cosine, hoyer,
projection) are evaluated on `pos_eval ∪ neg_eval` so the AUCs are
paired and the bootstrap + DeLong test see one observation per
classifier per pair. Cosine and Hoyer don't require a fit, but they
are still restricted to the eval split so the comparison is fair.

The pre-registration's `prohibited_actions` lists "evaluating any
score on pairs used to fit c_hat" first; this is the leakage guard.

## Determinism contract

  * Same `ComparisonConfig` + same input pairs → byte-identical
    `ComparisonReport.to_canonical_bytes()` output (modulo the
    volatile `generated_with` block recording library versions).
  * Bootstrap and 50/50 split index sampling are driven by
    `random.Random` seeded from `config.seed`; numpy is used only
    for the SVD inside `fit_c_hat`, the matrix-form `psi` of
    DeLong, and the score arithmetic — never for randomness.
  * The pre-registered seed (`random_seed: 49`) is hashed into
    `run_id` together with the corpus digest, bootstrap iteration
    count, and α so two reports with the same `run_id` are
    guaranteed identical.

## Operating procedure

```bash
# 1. Smoke test on the bundled tiny fixture (uses 200 bootstrap iters
#    so it finishes in seconds; the production count of 10 000 only
#    kicks in for real corpus runs).
python -m coherence_engine replication hoyer-vs-cosine --dry-run

# 2. Real run against a labeled pair-embedding corpus.
python -m coherence_engine replication hoyer-vs-cosine \
    --corpus path/to/pair_embeddings.json \
    --output reports/hoyer_vs_cosine.json
```

The corpus JSON shape is:

```json
{
  "schema": "hoyer-vs-cosine-fixture-v1",
  "model_id": "sentence-transformers/all-mpnet-base-v2",
  "model_version": "1.0.0",
  "dim": 768,
  "pairs": [
    {"label": "contradiction", "u": [...], "v": [...]},
    {"label": "entailment",    "u": [...], "v": [...]}
  ]
}
```

`neutral` rows are silently dropped per the pre-registration's
`excluded_labels` stanza — the binary task is contradiction vs
entailment.

## Synthetic-fixture sanity check

`fixtures/tiny_pair_fixture.json` carries 24 contradiction + 24
entailment pairs in dim=8. By construction:

  * Both labels' difference vectors `v - u` are orthogonal to `u`
    with magnitudes drawn from the same distribution → cosines
    overlap heavily across labels (cosine AUC ≈ 0.5).
  * Entailment deltas are dense (uniform direction); contradiction
    deltas are pure single-axis impulses → Hoyer cleanly separates
    (entailment max ≈ 0.37, contradiction min ≈ 0.38).

End-to-end on the dry-run config (seed=49, n_bootstrap=200,
α=0.01):

  * AUC(cosine)     ≈ 0.48
  * AUC(hoyer)      = 1.0
  * AUC(projection) ≈ 0.84
  * DeLong z(hoyer vs cosine) ≈ 4.18, p ≈ 2.9e-05  → reject H0
  * DeLong z(projection vs cosine) ≈ 2.74, p ≈ 6.1e-03 → reject H0
  * Decision: `hoyer_signal_differs_from_cosine`, winner = `hoyer`

The test suite re-asserts each of these (with thresholds rather than
exact equalities so cross-platform numerical jitter doesn't break
CI).

## Report blocks

```
schema_version  -> "hoyer-vs-cosine-v1"
run_id          -> sha256(corpus_digest | seed | n_boot | alpha)[:16]
config          -> seed, n_bootstrap, alpha, ci_percent, source
preregistration -> version, study_name, primary_hypothesis_id, alpha,
                   n_bootstrap, test
inputs          -> source, digest, model id/version, dim, n_pairs
                   total / fit / eval per label, c_hat_norm,
                   minimum_eval_pairs
auc             -> per-classifier {auc, ci_low, ci_high, ci_mean,
                   ci_std, ci_percent, n_bootstrap_iterations,
                   n_eval_positive, n_eval_negative}
delong          -> {hoyer_vs_cosine, projection_vs_cosine}, each
                   with {auc_a, auc_b, auc_diff, var_diff, z,
                   p_value, alpha, reject_null, test, winner,
                   null_hypothesis}
interpretation  -> primary_outcome, primary_winner, primary_p_value,
                   secondary_outcome, secondary_winner, alpha,
                   criterion, directionality
generated_with  -> python, numpy, scipy, sentence_transformers
                   versions (excluded from determinism comparisons)
```

## Limits

  * The bundled fixture is *synthetic*. A real production result
    requires a labeled pair-embedding corpus from the pre-registered
    encoder. The fixture exists to exercise the pipeline
    deterministically.
  * Cross-fit halves the effective sample. Real runs need ≥ 400
    pairs per label on disk to satisfy the `minimum_eval_pairs_per_label`
    stopping rule of 200 after the split.
  * DeLong's variance estimator assumes the eval set is fixed; if a
    future caller wants a CI on the AUC *difference* (rather than a
    test of equality), they should add a paired-bootstrap CI on
    `auc_diff` itself rather than reading it off DeLong's sandwich.

## Files

  * `Experiments/Hoyer_vs_Cosine/preregistration.yaml` — frozen
    study parameters
  * `Experiments/Hoyer_vs_Cosine/run_comparison.py` — deterministic
    harness + DeLong implementation
  * `Experiments/Hoyer_vs_Cosine/fixtures/tiny_pair_fixture.json` —
    synthetic 8-dim fixture used by `--dry-run` and the test suite
  * `tests/test_hoyer_vs_cosine.py` — 20 tests covering the
    statistical primitives, DeLong on the synthetic-dominance case,
    determinism, leakage guard, and CLI smoke
  * `cli.py` — `replication hoyer-vs-cosine` subcommand
