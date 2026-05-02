# Reverse Marxism — Reflection-Recovery Rigor Study (Wave 13, prompt 50/70)

## What we are testing

The original reverse-Marxism note (`apps/site/.../research/reverse_marxism.mdx`) reported a partial recovery story: Householder reflection across the class axis at α=2, applied to 50 hand-picked Manifesto sentences, decoded into ≈60% pro-market claims, ≈30% neutral descriptions, and ≈10% nonsense. A working interpretation circulated internally as "≈84.3% recovery". That number was the headline.

The question this study addresses is whether the headline survives **stricter conditions**:

  * **(a) held-out concept axes** — the class axis is fit on a training slab (Marx) and the recovery test is evaluated on a non-overlapping held-out slab of labeled sentences (Smith / Ricardo / Mill political-economy excerpts, with human-coded ideology labels);
  * **(b) bootstrapped CI** on the held-out recovery rate (10 000 iterations, 95% by default);
  * **(c) random-reflection null baseline** — `n_random_axes` (100) unit vectors are sampled uniformly on the sphere and the recovery rate is reported for each one. The empirical CI of that null distribution is the threshold the held-out recovery must clear;
  * **(d) α sensitivity sweep** — the headline pins α=2; the pre-registered grid `{0.5, 1.0, 1.5, 2.0, 2.5, 3.0}` is reported so a post-hoc α pick cannot inflate the headline.

If the held-out recovery at α=2 is within the random-baseline CI, **the reflection mechanism does not generalize** — and that finding is the publishable result.

## Operationalisation

| Quantity                     | Definition                                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| Reflection axis $\hat a$     | Mean of training `axis_seed_embeddings`, L2-normalised.                                           |
| Generalised reflection        | $v' = v - \alpha \cdot (v \cdot \hat a) \cdot \hat a$. α=2 is the standard Householder reflection. |
| Held-out sentence label       | `ideology_label ∈ {-1, +1}`. Hand-coded; **never** a function of the axis fit.                    |
| Recovery success per sentence | $\operatorname{sign}(v' \cdot \hat a) = -\,\text{ideology\_label}$.                                |
| Held-out recovery rate        | Fraction of held-out sentences with success = 1.                                                  |
| Random-axis baseline          | For each of `n_random_axes` random unit vectors $\hat r$, compute the held-out recovery rate using $\hat r$ in place of $\hat a$ in the reflection but always evaluating against $\hat a$. |
| Bootstrap CI (held-out)       | Resample held-out sentences with replacement; `n_bootstrap` iterations.                           |

The eval axis used to read off the post-reflection sign is the fitted axis $\hat a$ even for the random-axis baseline — otherwise the random-axis sign would be in a different coordinate frame from the labels.

## Falsification criterion

> **Confirm generalisation** at α=2 when the held-out recovery rate's lower 95% bootstrap CI exceeds the random-axis baseline's upper 95% CI at α=2.
>
> **Refute generalisation** at α=2 when the held-out recovery rate's lower CI is ≤ the random-axis baseline's upper CI at α=2.

Both outcomes are publishable per the negative-results policy — and refutation is what would make the rigor study load-bearing.

A note on the pre-registered "null = 0.5" idealisation: the random-axis baseline's *theoretical* mean is 0.5 only under the strong assumption that random reflections randomise the post-reflection projection sign. With finite-norm vectors and high embedding dimension, random unit vectors as reflection axes barely perturb the projection on the fitted axis — so empirically the baseline can land far from 0.5. The decision rule uses the **empirical baseline CI** (not the 0.5 idealisation), which is exactly the rigor study's value-add.

## Held-out protocol

> The axis is fit ONLY on `training_corpus.axis_seed_embeddings`; `holdout_corpus.sentences` are NEVER touched during axis fit. The recovery test is evaluated only on `holdout_corpus.sentences`.
>
> Mixing training and holdout sentences (either fitting the axis on holdout sentences or evaluating recovery on training sentences) is forbidden — the harness raises `HeldOutLeakageError` if it detects overlap (to 1e-8 precision) between the two slabs.

## Random-baseline protocol

> `n_random_axes` random unit vectors are sampled uniformly on the unit sphere in the encoder's output dimension; for each random axis we compute the held-out recovery rate at every α. The baseline distribution per α is summarised by mean, std, and 95% CI across the `n_random_axes` draws.

## Determinism contract

* `RIGOR_SCHEMA_VERSION = "reverse-marxism-rigor-v1"`.
* Same `RigorConfig` + same input corpus → byte-identical `to_canonical_bytes()` modulo `generated_with`.
* All sampling uses `random.Random` seeded from the pre-registration; `numpy` is used only for vector arithmetic, never for randomness.
* The α grid is canonicalised in keys as `f"alpha_{value:.4f}"` so the report bytes do not depend on float formatting.

## Operating procedure

* `python3 -m coherence_engine replication reverse-marxism-rigor --dry-run` — runs the bundled synthetic fixture (held-out recovery 1.0 at α=2, random baseline mean ≪ 0.5, decision: generalises).
* `python3 -m coherence_engine replication reverse-marxism-rigor --corpus path/to/corpus.json --output path/to/report.json` — runs against a pre-embedded held-out corpus.

### Corpus JSON shape

```json
{
  "schema": "reverse-marxism-rigor-fixture-v1",
  "model_id": "sentence-transformers/all-mpnet-base-v2",
  "model_version": "1.0.0",
  "dim": 768,
  "training_corpus": {
    "axis_seed_embeddings": [[...], ...]
  },
  "holdout_corpus": {
    "sentences": [
      {"embedding": [...], "ideology_label": -1},
      {"embedding": [...], "ideology_label":  1}
    ]
  }
}
```

A real run requires ≥5 axis seeds and ≥50 held-out sentences (per the `stopping_rule` in `preregistration.yaml`). The fixture relaxes those minima only when `source == "fixture"`.

## Synthetic fixture sanity check

`Experiments/Reverse_Marxism_Rigor/fixtures/tiny_rigor_fixture.json` — 8 axis seeds clustered along $e_0$, 60 held-out sentences (30 with positive projection on $e_0$, 30 with negative). Expected dry-run output:

* `held_out_recovery[alpha_2.0000].recovery_rate` = 1.0
* `random_baseline[alpha_2.0000].mean` ≈ 0.001 (well below 0.5)
* `decision.primary_generalises` = `true`
* `decision.primary_outcome` = `reflection_recovery_generalises_held_out`

## Limits

* The synthetic fixture does **not** demonstrate that the headline 84.3% number generalises on real text — it demonstrates that the **harness** correctly distinguishes a well-defined-flipping case from a random-axis null. A real run on encoded political-economy excerpts is the next step.
* Recovery is operationalised via projection sign on the fitted axis. The original mdx note used nearest-neighbour decoding into a 500K-sentence reference corpus, which is more permissive but harder to make deterministic. Sign-on-axis is the conservative replacement.
* The α grid is fixed at `{0.5, 1.0, 1.5, 2.0, 2.5, 3.0}`. Bumping requires a new `version:` in the pre-registration plus a justification stanza.

## File index

* `Experiments/Reverse_Marxism_Rigor/preregistration.yaml` — frozen study parameters.
* `Experiments/Reverse_Marxism_Rigor/run_rigor_study.py` — deterministic harness.
* `Experiments/Reverse_Marxism_Rigor/fixtures/tiny_rigor_fixture.json` — synthetic dry-run fixture.
* `tests/test_reverse_marxism_rigor.py` — 28 tests covering reflection math, fit / recovery primitives, bootstrap, pre-registration parsing, end-to-end on the fixture, leakage detection, stopping rule, schema-version pin, CLI smoke.
* `cli.py` — `replication reverse-marxism-rigor` verb.
