"""Reverse-Marxism reflection-recovery rigor study.

Prompt 50, Wave 13. Replicates the headline 84.3% Householder-
reflection recovery claim under stricter conditions:

  (a) **held-out concept axes** — the class axis is fit on a training
      slab (axis seed embeddings) and the recovery test is evaluated
      on a non-overlapping held-out slab of labeled sentences.
  (b) **bootstrap CI** on the held-out recovery rate (10 000
      iterations, 95% by default).
  (c) **random-reflection null baseline** — n_random_axes unit vectors
      are sampled uniformly on the sphere and the recovery rate is
      reported for each one. The empirical CI of that null
      distribution is the threshold the held-out recovery must clear.
  (d) **alpha sensitivity sweep** — the headline pins alpha=2; the
      pre-registered grid {0.5, 1.0, 1.5, 2.0, 2.5, 3.0} is reported
      so a post-hoc alpha pick cannot inflate the headline.

Reflection formula
------------------

The generalised Householder reflection at sensitivity alpha is::

    v_prime = v - alpha * dot(v, axis) * axis

For ``alpha == 2`` this is the standard reflection across the
hyperplane orthogonal to ``axis``. For ``alpha < 1`` the projection
along axis is dampened; for ``alpha == 1`` the projection is removed
(a pure orthogonal projection); for ``alpha > 2`` the projection is
amplified. The grid is fixed in ``preregistration.yaml`` so post-hoc
alpha selection is impossible.

Recovery operationalisation
---------------------------

Each held-out sentence carries ``ideology_label`` in ``{-1, +1}``
that was set independently of the axis fit (e.g., "pro-labour" vs
"pro-capital"). Recovery for a sentence is::

    success = sign(dot(reflect(v, axis_fit, alpha), axis_fit))
              == -ideology_label

i.e., reflection is expected to flip the sentence to the side opposite
its original ideological orientation. The recovery rate over the
held-out slab is the fraction of successes.

Determinism guarantees mirror the cosine-paradox / c-hat-stability /
hoyer-vs-cosine harnesses: the YAML parser is the tiny stdlib subset
used there, all index sampling and random-axis sampling use
``random.Random`` seeded from the pre-registration, and the canonical
JSON report is identical given identical inputs and seeds. ``numpy``
is used only for vector arithmetic, never for randomness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from coherence_engine.Experiments.Cosine_Paradox_Replication.run_replication import (
    _detect_optional_packages,
    _parse_yaml,
    _percentile,
    _round_floats,
    PreregistrationError,
    ReplicationError,
)


RIGOR_SCHEMA_VERSION = "reverse-marxism-rigor-v1"

_HERE = Path(__file__).resolve().parent
DEFAULT_PREREGISTRATION_PATH = _HERE / "preregistration.yaml"
DEFAULT_FIXTURE_PATH = _HERE / "fixtures" / "tiny_rigor_fixture.json"

_DEFAULT_ALPHA_GRID: Tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
_HEADLINE_ALPHA = 2.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RigorError(ReplicationError):
    """Base class for reverse-marxism rigor harness failures."""


class InsufficientHoldoutError(RigorError):
    def __init__(self, n: int, minimum: int):
        self.n = int(n)
        self.minimum = int(minimum)
        super().__init__(
            f"INSUFFICIENT_SAMPLE: holdout sentences n={n} < "
            f"minimum={minimum}; refusing to emit report"
        )


class InsufficientAxisSeedsError(RigorError):
    def __init__(self, n: int, minimum: int):
        self.n = int(n)
        self.minimum = int(minimum)
        super().__init__(
            f"INSUFFICIENT_SAMPLE: axis_seed_embeddings n={n} < "
            f"minimum={minimum}; refusing to emit report"
        )


class HeldOutLeakageError(RigorError):
    """Raised when training and holdout slabs share an embedding."""


# ---------------------------------------------------------------------------
# Pre-registration loading
# ---------------------------------------------------------------------------


def load_preregistration(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    p = Path(path) if path is not None else DEFAULT_PREREGISTRATION_PATH
    text = p.read_text(encoding="utf-8")
    parsed = _parse_yaml(text)
    required = (
        "version", "study_name", "dataset", "embedding_model",
        "axis_construction", "reflection", "alpha_grid",
        "primary_hypothesis", "n_random_axes", "n_bootstrap",
        "ci_percent", "random_seed", "stopping_rule",
        "held_out_protocol", "random_baseline",
    )
    missing = [k for k in required if k not in parsed]
    if missing:
        raise PreregistrationError(
            f"preregistration is missing required keys: {missing}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def load_rigor_corpus(path: os.PathLike) -> Dict[str, Any]:
    """Load the held-out rigor corpus.

    Schema::

        {
          "schema": "reverse-marxism-rigor-fixture-v1",
          "model_id": ...,
          "model_version": ...,
          "dim": <int>,
          "training_corpus": {
            "axis_seed_embeddings": [[...], ...]
          },
          "holdout_corpus": {
            "sentences": [{"embedding": [...], "ideology_label": -1|+1}, ...]
          }
        }
    """
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    dim = payload.get("dim")
    if not isinstance(dim, int) or dim <= 0:
        raise RigorError(f"{p}: 'dim' must be a positive integer")
    train = payload.get("training_corpus") or {}
    seeds_raw = train.get("axis_seed_embeddings")
    if not isinstance(seeds_raw, list) or not seeds_raw:
        raise RigorError(
            f"{p}: training_corpus.axis_seed_embeddings must be a "
            "non-empty list of length-dim float lists"
        )
    seeds: List[List[float]] = []
    for row in seeds_raw:
        if not isinstance(row, list) or len(row) != dim:
            raise RigorError(
                f"{p}: every axis_seed_embedding must be a list of "
                f"length dim={dim}"
            )
        seeds.append([float(x) for x in row])
    holdout_block = payload.get("holdout_corpus") or {}
    sentences_raw = holdout_block.get("sentences")
    if not isinstance(sentences_raw, list) or not sentences_raw:
        raise RigorError(
            f"{p}: holdout_corpus.sentences must be a non-empty list"
        )
    holdout: List[Dict[str, Any]] = []
    for row in sentences_raw:
        emb = row.get("embedding")
        label = row.get("ideology_label")
        if not isinstance(emb, list) or len(emb) != dim:
            raise RigorError(
                f"{p}: every holdout sentence needs an 'embedding' "
                f"list of length dim={dim}"
            )
        if label not in (-1, 1):
            raise RigorError(
                f"{p}: every holdout sentence needs ideology_label in "
                f"{{-1, +1}}; got {label!r}"
            )
        holdout.append(
            {"embedding": [float(x) for x in emb], "ideology_label": int(label)}
        )
    return {
        "source": payload.get("source", "unknown"),
        "model_id": payload.get("model_id"),
        "model_version": payload.get("model_version"),
        "schema": payload.get("schema"),
        "dim": dim,
        "training_corpus": {"axis_seed_embeddings": seeds},
        "holdout_corpus": {"sentences": holdout},
    }


def _check_held_out_leakage(
    seeds: Sequence[Sequence[float]],
    holdout: Sequence[Mapping[str, Any]],
) -> None:
    """Refuse to run if any axis seed shows up verbatim in the holdout slab."""
    seed_keys = {tuple(round(x, 8) for x in s) for s in seeds}
    for entry in holdout:
        key = tuple(round(x, 8) for x in entry["embedding"])
        if key in seed_keys:
            raise HeldOutLeakageError(
                "training axis-seed embedding overlaps holdout sentence "
                "embedding to 1e-8 precision; the held-out protocol "
                "requires disjoint slabs"
            )


# ---------------------------------------------------------------------------
# Reflection math
# ---------------------------------------------------------------------------


def fit_axis_from_seeds(seed_embeddings: Sequence[Sequence[float]]) -> np.ndarray:
    """Average the seed embeddings and L2-normalise."""
    arr = np.asarray(list(seed_embeddings), dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] < 1:
        raise RigorError("fit_axis_from_seeds: need >=1 seed embedding")
    centroid = arr.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm <= 0.0:
        raise RigorError(
            "fit_axis_from_seeds: zero-norm centroid; check seed terms"
        )
    return centroid / norm


def householder_reflect(
    v: np.ndarray, axis: np.ndarray, alpha: float
) -> np.ndarray:
    """Generalised Householder reflection: v - alpha * (v . axis) * axis."""
    return v - float(alpha) * float(np.dot(v, axis)) * axis


def _sign(x: float) -> int:
    if x > 0.0:
        return 1
    if x < 0.0:
        return -1
    # treat zero as +1 so the canonical bytes never depend on a tie
    return 1


def recovery_rate(
    holdout: Sequence[Mapping[str, Any]],
    *,
    reflect_axis: np.ndarray,
    alpha: float,
    eval_axis: np.ndarray,
) -> Tuple[float, List[int]]:
    """Recovery rate + per-sentence success indicator vector.

    A sentence is a success when the sign of its reflected projection
    on ``eval_axis`` matches ``-ideology_label`` — i.e., reflection is
    expected to flip the sentence to the side opposite its original
    ideological orientation.
    """
    successes: List[int] = []
    for entry in holdout:
        v = np.asarray(entry["embedding"], dtype=np.float64)
        v_prime = householder_reflect(v, reflect_axis, alpha)
        new_proj = float(np.dot(v_prime, eval_axis))
        expected_sign = -int(entry["ideology_label"])
        successes.append(1 if _sign(new_proj) == expected_sign else 0)
    n = len(successes)
    rate = (sum(successes) / n) if n else 0.0
    return rate, successes


def sample_random_unit_vector(rng: random.Random, dim: int) -> np.ndarray:
    """Uniform on S^{dim-1}: Gaussian then L2-normalise."""
    while True:
        v = np.array([rng.gauss(0.0, 1.0) for _ in range(dim)], dtype=np.float64)
        n = float(np.linalg.norm(v))
        if n > 1e-12:
            return v / n


# ---------------------------------------------------------------------------
# Statistics — bootstrap + summaries
# ---------------------------------------------------------------------------


def bootstrap_recovery_ci(
    successes: Sequence[int],
    *,
    iterations: int,
    seed: int,
    ci: float = 95.0,
) -> Tuple[float, float, float, float]:
    """Bootstrap CI on the recovery rate (mean of 0/1 successes)."""
    n = len(successes)
    if n == 0:
        return 0.0, 0.0, 0.0, 0.0
    rng = random.Random(seed)
    samples: List[float] = []
    for _ in range(iterations):
        s = 0
        for _ in range(n):
            s += successes[rng.randrange(n)]
        samples.append(s / n)
    samples.sort()
    alpha_pct = (100.0 - ci) / 2.0
    low = _percentile(samples, alpha_pct)
    high = _percentile(samples, 100.0 - alpha_pct)
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / max(1, len(samples) - 1)
    std = math.sqrt(var)
    return low, high, mean, std


def summarise_distribution(values: Sequence[float], *, ci: float = 95.0) -> Dict[str, float]:
    if not values:
        return {
            "n": 0, "mean": 0.0, "std": 0.0,
            "ci_low": 0.0, "ci_high": 0.0, "min": 0.0, "max": 0.0,
            "ci_percent": ci,
        }
    sorted_v = sorted(values)
    n = len(values)
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / max(1, n - 1)
    alpha_pct = (100.0 - ci) / 2.0
    return {
        "n": n,
        "mean": mean,
        "std": math.sqrt(var),
        "min": float(sorted_v[0]),
        "max": float(sorted_v[-1]),
        "ci_low": _percentile(sorted_v, alpha_pct),
        "ci_high": _percentile(sorted_v, 100.0 - alpha_pct),
        "ci_percent": ci,
    }


# ---------------------------------------------------------------------------
# Configuration + report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RigorConfig:
    seed: int = 50
    n_random_axes: int = 100
    n_bootstrap: int = 10_000
    ci_percent: float = 95.0
    alpha_grid: Tuple[float, ...] = _DEFAULT_ALPHA_GRID
    fixture_path: Optional[str] = None
    corpus_path: Optional[str] = None
    preregistration_path: Optional[str] = None
    minimum_holdout_override: Optional[int] = None
    minimum_axis_seeds_override: Optional[int] = None


@dataclass
class RigorReport:
    schema_version: str
    run_id: str
    config: Dict[str, Any]
    preregistration: Dict[str, Any]
    inputs: Dict[str, Any]
    held_out_recovery: Dict[str, Any]
    random_baseline: Dict[str, Any]
    decision: Dict[str, Any]
    generated_with: Dict[str, Any]

    def to_canonical_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "config": self.config,
            "preregistration": self.preregistration,
            "inputs": self.inputs,
            "held_out_recovery": self.held_out_recovery,
            "random_baseline": self.random_baseline,
            "decision": self.decision,
            "generated_with": self.generated_with,
        }
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    def to_canonical_dict(self) -> Dict[str, Any]:
        return json.loads(self.to_canonical_bytes().decode("ascii"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _digest_corpus(
    seeds: Sequence[Sequence[float]],
    holdout: Sequence[Mapping[str, Any]],
) -> str:
    h = hashlib.sha256()
    h.update(b"seeds:")
    for s in seeds:
        for x in s:
            h.update(f"{x:.10f}".encode("ascii"))
            h.update(b",")
        h.update(b"|")
    h.update(b"holdout:")
    for entry in holdout:
        h.update(f"{int(entry['ideology_label'])}".encode("ascii"))
        h.update(b":")
        for x in entry["embedding"]:
            h.update(f"{x:.10f}".encode("ascii"))
            h.update(b",")
        h.update(b"|")
    return h.hexdigest()


def _resolve_minimum(
    prereg: Mapping[str, Any], key: str, override: Optional[int], default: int
) -> int:
    if override is not None:
        return int(override)
    rule = prereg.get("stopping_rule") or {}
    return int(rule.get(key, default))


def _parse_alpha_grid(raw: Any) -> Tuple[float, ...]:
    if raw is None:
        return _DEFAULT_ALPHA_GRID
    out: List[float] = []
    for v in raw:
        out.append(float(v))
    return tuple(out)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_rigor_study(config: RigorConfig) -> RigorReport:
    prereg = load_preregistration(config.preregistration_path)
    seed = int(config.seed)

    if config.fixture_path:
        corpus = load_rigor_corpus(config.fixture_path)
        corpus_source = "fixture"
    elif config.corpus_path:
        corpus = load_rigor_corpus(config.corpus_path)
        corpus_source = corpus.get("source") or "corpus_json"
    else:
        raise RigorError(
            "RigorConfig: must provide one of fixture_path or corpus_path"
        )

    seeds_emb = corpus["training_corpus"]["axis_seed_embeddings"]
    holdout = corpus["holdout_corpus"]["sentences"]
    dim = int(corpus["dim"])

    # Stopping rule (relaxed for fixtures so dry-run + tests can run on
    # tiny synthetic slabs).
    minimum_holdout = _resolve_minimum(
        prereg, "minimum_holdout_sentences",
        config.minimum_holdout_override, 50,
    )
    minimum_axis_seeds = _resolve_minimum(
        prereg, "minimum_axis_seeds",
        config.minimum_axis_seeds_override, 5,
    )
    if corpus_source != "fixture":
        if len(holdout) < minimum_holdout:
            raise InsufficientHoldoutError(len(holdout), minimum_holdout)
        if len(seeds_emb) < minimum_axis_seeds:
            raise InsufficientAxisSeedsError(
                len(seeds_emb), minimum_axis_seeds
            )

    _check_held_out_leakage(seeds_emb, holdout)

    # ── 1. Fit the axis on the training slab (seeds only) ─────────
    axis = fit_axis_from_seeds(seeds_emb)

    # ── 2. Held-out recovery: per alpha, point + bootstrap CI ──────
    boot_seed = seed + 7919
    held_out: Dict[str, Any] = {}
    for alpha in config.alpha_grid:
        rate, successes = recovery_rate(
            holdout, reflect_axis=axis, alpha=float(alpha), eval_axis=axis,
        )
        ci_low, ci_high, ci_mean, ci_std = bootstrap_recovery_ci(
            successes,
            iterations=int(config.n_bootstrap),
            seed=boot_seed + int(round(float(alpha) * 1000)),
            ci=float(config.ci_percent),
        )
        held_out[_alpha_key(alpha)] = {
            "alpha": float(alpha),
            "recovery_rate": rate,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "ci_mean": ci_mean,
            "ci_std": ci_std,
            "ci_percent": float(config.ci_percent),
            "n_bootstrap_iterations": int(config.n_bootstrap),
            "n_holdout": len(holdout),
            "n_successes": int(sum(successes)),
        }

    # ── 3. Random-axis null distribution ────────────────────────────
    rand_rng = random.Random(seed + 31337)
    random_axes = [
        sample_random_unit_vector(rand_rng, dim)
        for _ in range(int(config.n_random_axes))
    ]
    baseline_per_alpha: Dict[str, Any] = {}
    for alpha in config.alpha_grid:
        rates: List[float] = []
        for r_axis in random_axes:
            rate_r, _ = recovery_rate(
                holdout,
                reflect_axis=r_axis,
                alpha=float(alpha),
                eval_axis=axis,  # evaluation axis is always the fitted axis
            )
            rates.append(rate_r)
        summary = summarise_distribution(rates, ci=float(config.ci_percent))
        baseline_per_alpha[_alpha_key(alpha)] = {
            "alpha": float(alpha),
            "n_random_axes": int(config.n_random_axes),
            **summary,
        }

    # ── 4. Decision ────────────────────────────────────────────────
    headline_key = _alpha_key(_HEADLINE_ALPHA)
    held_out_headline = held_out.get(headline_key)
    baseline_headline = baseline_per_alpha.get(headline_key)
    if held_out_headline is None or baseline_headline is None:
        generalises = False
        margin = 0.0
    else:
        generalises = bool(
            held_out_headline["ci_low"] > baseline_headline["ci_high"]
        )
        margin = (
            held_out_headline["ci_low"] - baseline_headline["ci_high"]
        )

    secondary_pass = False
    a05 = held_out.get(_alpha_key(0.5))
    a10 = held_out.get(_alpha_key(1.0))
    if held_out_headline is not None and a05 is not None and a10 is not None:
        worst_low = max(a05["recovery_rate"], a10["recovery_rate"])
        baseline_std = (
            baseline_headline["std"] if baseline_headline is not None else 0.0
        )
        secondary_pass = bool(
            held_out_headline["recovery_rate"] - worst_low > baseline_std
        )

    decision = {
        "headline_alpha": _HEADLINE_ALPHA,
        "primary_criterion": (
            "held_out_recovery_ci_low > random_baseline_ci_high "
            "at headline_alpha"
        ),
        "primary_held_out_ci_low": (
            held_out_headline["ci_low"] if held_out_headline else 0.0
        ),
        "primary_random_baseline_ci_high": (
            baseline_headline["ci_high"] if baseline_headline else 0.0
        ),
        "primary_margin": margin,
        "primary_generalises": generalises,
        "primary_outcome": (
            "reflection_recovery_generalises_held_out"
            if generalises
            else "reflection_recovery_does_not_generalise_held_out"
        ),
        "secondary_criterion": (
            "headline_alpha recovery exceeds max(alpha=0.5, alpha=1.0) "
            "by more than the random-baseline std at headline_alpha"
        ),
        "secondary_pass": secondary_pass,
    }

    # ── 5. Inputs / config / preregistration ───────────────────────
    inputs_digest = _digest_corpus(seeds_emb, holdout)
    inputs = {
        "corpus_source": corpus_source,
        "corpus_digest_sha256": inputs_digest,
        "model_id": corpus.get("model_id"),
        "model_version": corpus.get("model_version"),
        "dim": dim,
        "n_axis_seeds": len(seeds_emb),
        "n_holdout_sentences": len(holdout),
        "n_holdout_positive": sum(1 for s in holdout if s["ideology_label"] == 1),
        "n_holdout_negative": sum(1 for s in holdout if s["ideology_label"] == -1),
        "axis_norm": float(np.linalg.norm(axis)),
        "minimum_holdout_sentences": minimum_holdout,
        "minimum_axis_seeds": minimum_axis_seeds,
    }

    config_for_report = {
        "seed": seed,
        "n_random_axes": int(config.n_random_axes),
        "n_bootstrap": int(config.n_bootstrap),
        "ci_percent": float(config.ci_percent),
        "alpha_grid": [float(a) for a in config.alpha_grid],
        "corpus_source": corpus_source,
    }

    run_id = hashlib.sha256(
        f"{inputs_digest}|seed={seed}|"
        f"boot={config.n_bootstrap}|"
        f"axes={config.n_random_axes}|"
        f"alphas={list(config.alpha_grid)}".encode("ascii")
    ).hexdigest()[:16]

    primary_pre = prereg.get("primary_hypothesis") or {}
    secondary_pre = prereg.get("secondary_hypothesis") or {}
    report = RigorReport(
        schema_version=RIGOR_SCHEMA_VERSION,
        run_id=run_id,
        config=config_for_report,
        preregistration={
            "version": prereg.get("version"),
            "study_name": prereg.get("study_name"),
            "primary_hypothesis_id": primary_pre.get("id"),
            "secondary_hypothesis_id": secondary_pre.get("id"),
            "alpha_grid": list(prereg.get("alpha_grid") or []),
            "n_random_axes": prereg.get("n_random_axes"),
            "n_bootstrap": prereg.get("n_bootstrap"),
            "ci_percent": prereg.get("ci_percent"),
            "headline_alpha": _HEADLINE_ALPHA,
        },
        inputs=inputs,
        held_out_recovery=_round_floats({
            "by_alpha": held_out,
            "headline_alpha": _HEADLINE_ALPHA,
        }),
        random_baseline=_round_floats({
            "by_alpha": baseline_per_alpha,
            "headline_alpha": _HEADLINE_ALPHA,
            "n_random_axes": int(config.n_random_axes),
        }),
        decision=_round_floats(decision),
        generated_with=_detect_optional_packages(),
    )
    return report


def _alpha_key(alpha: float) -> str:
    """Stable canonical string key for an alpha value (so report bytes
    are deterministic regardless of float formatting)."""
    return f"alpha_{float(alpha):.4f}"


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reverse-marxism-rigor",
        description=(
            "Reverse-Marxism reflection-recovery rigor study "
            "(prompt 50, Wave 13)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on the bundled synthetic fixture and emit JSON to stdout.",
    )
    parser.add_argument(
        "--corpus",
        dest="corpus_path",
        type=str,
        default=None,
        help="Path to a held-out rigor-corpus JSON.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write the canonical JSON report here (default: stdout).",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Override the pre-registered random seed.",
    )
    parser.add_argument(
        "--n-bootstrap", type=int, default=None,
        help="Override the bootstrap iteration count.",
    )
    parser.add_argument(
        "--n-random-axes", type=int, default=None,
        help="Override the number of random null-baseline axes.",
    )
    parser.add_argument(
        "--preregistration", type=str, default=None,
        help="Override path to preregistration.yaml.",
    )
    parser.add_argument(
        "--minimum-holdout-sentences", type=int, default=None,
        help="Override stopping_rule.minimum_holdout_sentences (testing only).",
    )
    parser.add_argument(
        "--minimum-axis-seeds", type=int, default=None,
        help="Override stopping_rule.minimum_axis_seeds (testing only).",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> RigorConfig:
    prereg = load_preregistration(args.preregistration)
    seed = (
        args.seed if args.seed is not None
        else int(prereg.get("random_seed") or 50)
    )
    n_boot = (
        args.n_bootstrap
        if args.n_bootstrap is not None
        else int(prereg.get("n_bootstrap") or 10_000)
    )
    n_rand = (
        args.n_random_axes
        if args.n_random_axes is not None
        else int(prereg.get("n_random_axes") or 100)
    )
    alpha_grid = _parse_alpha_grid(prereg.get("alpha_grid"))
    ci = float(prereg.get("ci_percent") or 95.0)

    if args.dry_run:
        # Smaller iteration counts so dry-run finishes in a couple
        # seconds against the bundled fixture. Production runs use the
        # full pre-registered values.
        return RigorConfig(
            seed=seed,
            n_random_axes=args.n_random_axes or 32,
            n_bootstrap=args.n_bootstrap or 200,
            ci_percent=ci,
            alpha_grid=alpha_grid,
            fixture_path=str(DEFAULT_FIXTURE_PATH),
            preregistration_path=args.preregistration,
            minimum_holdout_override=(
                args.minimum_holdout_sentences or 4
            ),
            minimum_axis_seeds_override=(
                args.minimum_axis_seeds or 2
            ),
        )
    return RigorConfig(
        seed=seed,
        n_random_axes=n_rand,
        n_bootstrap=n_boot,
        ci_percent=ci,
        alpha_grid=alpha_grid,
        corpus_path=args.corpus_path,
        preregistration_path=args.preregistration,
        minimum_holdout_override=args.minimum_holdout_sentences,
        minimum_axis_seeds_override=args.minimum_axis_seeds,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    if not args.dry_run and not args.corpus_path:
        sys.stderr.write(
            "error: must pass one of --dry-run or --corpus\n"
        )
        return 2
    config = _config_from_args(args)
    try:
        report = run_rigor_study(config)
    except (InsufficientHoldoutError, InsufficientAxisSeedsError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return 3
    except RigorError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    canonical = report.to_canonical_bytes()
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(canonical)
    sys.stdout.write(canonical.decode("ascii"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
