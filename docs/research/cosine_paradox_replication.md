# Cosine Paradox — independent replication harness

**Prompt 47 (Wave 13).** This document describes the design, the
falsification criterion, and the operating procedure for the
independent replication of the *Cosine Paradox* working paper
(`apps/site/src/content/research/cosine_paradox.mdx`).

The original paper used 400 hand-constructed sentence pairs and
reported that — in raw `all-mpnet-base-v2` cosine space — direction-
flipped contradictions and topic-shifted controls land at
indistinguishable similarity. The headline summary that gets quoted in
practice is the *stronger* claim: that **entailment and contradiction
NLI pairs are statistically indistinguishable in raw cosine
similarity**. This replication tests that stronger, more useful
claim on a labeled NLI corpus with a frozen pre-registration.

This is *not* the original cosine-paradox paper, and it is not a
re-print. It is an adversarial replication — its job is to either
independently confirm the headline on data the original authors did
not pick, or to refute it.

## What gets replicated

| Concept                        | Original paper                         | This replication                                                |
|--------------------------------|----------------------------------------|-----------------------------------------------------------------|
| Encoder                        | `all-mpnet-base-v2`                    | same (pinned in `preregistration.yaml`)                         |
| Corpus                         | 400 hand-constructed sentence pairs    | independent labeled NLI corpus (SNLI dev, MultiNLI fallback)    |
| Test statistic                 | mean cosine per group                  | Mann-Whitney U (rank-biserial effect size + permutation p)      |
| Sample size                    | 400 synthetic pairs                    | ≥ 200 per label (pre-registered stopping rule)                  |
| Effect-size measure            | difference of group means              | rank-biserial — robust to scale + ties                          |
| CI                             | none                                   | 95 % bootstrap CI on the rank-biserial (n=10000 stdlib)         |
| Inferential test               | none                                   | two-sided permutation test (n=10000)                            |
| Pre-registration               | none                                   | `preregistration.yaml` v1.0, frozen                             |
| Negative-results policy        | not stated                             | publish in either direction                                     |

## Falsification criterion

> The original Cosine Paradox claim is **refuted** when the rank-biserial
> effect size between entailment and contradiction has magnitude
> ≥ 0.20 *and* the two-sided permutation test rejects H0 at α = 0.01.
>
> Otherwise the claim is **confirmed**.

The threshold of 0.20 is the conventional "small-but-non-trivial"
boundary for a rank-correlation effect. The α and n_permutations
values were registered before any data run; bumping either requires
a new `version:` in `preregistration.yaml` with a written
`justification:` stanza and is auditable in version control.

The harness emits the same canonical-JSON report shape regardless
of which way the test lands. A refutation result *will* be published.

## Operating procedure

```bash
# 1. Smoke test on the bundled tiny fixture (uses 1000 perm + 1000 boot
#    iterations so it finishes in seconds; the production thresholds
#    pinned in preregistration.yaml only kick in for real corpus runs).
python -m coherence_engine replication cosine-paradox --dry-run

# 2. Real replication run. The harness refuses to embed without
#    --allow-network, since the corpus + model would need to download.
#    Best practice: pre-stage the corpus locally and call --cosines.
python -m coherence_engine replication cosine-paradox \
    --dataset data/replication/snli_1.0_dev.jsonl \
    --output  data/replication/report.json \
    --allow-network

# 3. Replay-only (no embedding) once cosines are computed. This is
#    the path that the test suite exercises.
python -m coherence_engine replication cosine-paradox \
    --cosines data/replication/snli_dev_cosines.json \
    --output  data/replication/report.json

# 4. Unit + integration tests
python -m pytest tests/test_cosine_paradox_replication.py -v
```

## Determinism contract

* Same `ReplicationConfig` (seed + same cosines file) → byte-identical
  `ReplicationReport.to_canonical_bytes()`.
* Bootstrap + permutation tests use `random.Random(seed)` from the
  Python standard library — no numpy. Same seed → same sample
  sequence on every machine.
* `inputs.cosines_digest_sha256` records a SHA-256 of the per-label
  cosine arrays so that any drift in the upstream embedding pipeline
  changes the report's digest *visibly*.
* `run_id` is a 16-char prefix of `sha256(digest | seed | iter
  counts | alpha)` — copy-paste-able and stable across runs.
* No wall-clock reads, no live database reads, no implicit network
  reads. The harness *refuses* to touch the network unless
  `--allow-network` is explicit.

## Statistical methods (stdlib, no scipy)

* **Mann-Whitney U.** Combined ranking with average ranks for ties.
  `U_a = R_a − n1·(n1+1)/2`. We report both `U_a`, `U_b`, and
  `min(U_a, U_b)`.
* **Rank-biserial effect size.** Wendt's formula: `r = 2·U_a /
  (n1·n2) − 1`. Range [-1, +1]. Sign convention: positive when
  group A (entailment) ranks above group B (contradiction).
* **95 % bootstrap CI.** Stratified resample with replacement (n1
  draws from group A, n2 from group B). Default n=10000. The
  reported CI is the 2.5 / 97.5 percentile of the resampled
  effect-size distribution, computed via linear interpolation
  between adjacent ranks.
* **Two-sided permutation p-value.** Pool, shuffle, recompute U,
  count ties using `|U − n1·n2/2|` as the test statistic. We use
  the additive-smoothing variant `(count + 1) / (n_perm + 1)` so
  the p-value is a strict permutation p (never zero, never one).

## Leakage caveat

`all-mpnet-base-v2` was pre-trained on roughly one billion sentence
pairs sampled from Reddit, S2ORC, WikiAnswers, Stack Exchange, and
several smaller sources. SNLI captions (originating from Flickr30k)
and MultiNLI's prompt corpora are not enumerated in the model card,
but cannot be ruled out as part of the pre-training mix. We
therefore document this as a *replication on plausibly-overlapping
data* rather than a clean held-out test (see
`preregistration.yaml.leakage_assumption`). A future strengthening
step is to re-run on a corpus published *after* the model snapshot
date — the harness is already structured to accept any
premise/hypothesis/label .jsonl, so adding a new corpus is a
configuration change, not a code change.

We deliberately do **not** fine-tune or otherwise adapt the
embedder on the replication dataset. Doing so would be a flagrant
violation of the prohibitions list in the pre-registration.

## Files

| Path                                                                  | Purpose                                               |
|-----------------------------------------------------------------------|-------------------------------------------------------|
| `Experiments/Cosine_Paradox_Replication/run_replication.py`           | The harness module + ad-hoc CLI                       |
| `Experiments/Cosine_Paradox_Replication/preregistration.yaml`         | Frozen study parameters                               |
| `Experiments/Cosine_Paradox_Replication/expected_report.json`         | Pinned baseline produced by the dry-run               |
| `Experiments/Cosine_Paradox_Replication/fixtures/tiny_nli_fixture.json` | Tiny seeded NLI cosines for tests + dry-run         |
| `tests/test_cosine_paradox_replication.py`                            | Unit + integration tests (18 cases)                   |
| `cli.py` (`replication cosine-paradox` subcommand)                    | First-class CLI entry from the engine binary          |

## Out of scope

* Building a *better* contradiction signal than cosine. That is the
  job of the companion paper [`contradiction_direction`](./../../apps/site/src/content/research/contradiction_direction.mdx),
  not this replication.
* Fine-tuning the embedder. Explicitly prohibited by the
  pre-registration.
* Running the full pipeline against multiple encoders. A single
  encoder per replication study, version pinned. Encoder-dependence
  is a separate study and would carry its own pre-registration.
