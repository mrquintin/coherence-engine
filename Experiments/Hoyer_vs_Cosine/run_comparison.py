"""Hoyer-sparsity vs raw-cosine head-to-head ROC harness.

Prompt 49, Wave 13. Depends on the cosine-paradox harness for the
shared YAML parser, percentile, ranks-with-ties, and round-floats
helpers and on ``core.contradiction_direction`` for ``fit_c_hat`` /
``project``.

Pipeline:

  1. Load a labeled pair-embedding corpus (or the bundled fixture) of
     premise/hypothesis pairs labeled ``contradiction`` (positive) or
     ``entailment`` (negative). Neutral is excluded by the
     pre-registration.
  2. Deterministically split BOTH labels 50/50 into a ``fit`` half
     and an ``eval`` half using ``random.Random(seed)``.
  3. Fit ĉ on the fit-half of contradiction pairs only.
  4. Compute three scores per pair on the eval-half (paired across
     classifiers, so DeLong's variance estimator is meaningful):

        cosine_score(u, v)     = 1 - cosine_similarity(u, v)
        hoyer_score(u, v)      = hoyer_sparsity(u - v)
        projection_score(u, v) = |⟨u - v, ĉ⟩|

  5. Report ROC AUC + 95 % paired-bootstrap CI for each score.
  6. Run DeLong's two-sided z-test for AUC equality on
     ``(cosine, hoyer)`` and ``(cosine, projection)``.
  7. Emit a deterministic JSON report whose canonical bytes are
     stable across runs given the same seed and the same input
     pairs (modulo ``generated_with``).

Determinism guarantees mirror the cosine-paradox + ĉ-stability
harnesses: the YAML parser is the tiny stdlib subset used there;
all index sampling uses ``random.Random`` seeded from the
pre-registration; bootstrap CI endpoints are reported to 1e-10
precision after a final rounding pass.

DeLong test
-----------

We implement the standard DeLong, DeLong & Clarke-Pearson (1988)
estimator directly so the harness has no scipy dependency. For each
classifier r and an evaluation set with positives ``X[r]`` (length
``n_pos``) and negatives ``Y[r]`` (length ``n_neg``):

    psi(a, b) = 1.0      if a > b
              = 0.5      if a == b
              = 0.0      if a < b

    V10[r][i] = (1 / n_neg) * sum_j psi(X[r][i], Y[r][j])
    V01[r][j] = (1 / n_pos) * sum_i psi(X[r][i], Y[r][j])
    AUC[r]    = mean(V10[r]) = mean(V01[r])

The covariance matrices are::

    S10[r,s] = cov(V10[r], V10[s])     # over the n_pos eval positives
    S01[r,s] = cov(V01[r], V01[s])     # over the n_neg eval negatives
    S        = S10 / n_pos + S01 / n_neg

For the paired test of AUC[r] == AUC[s]::

    z = (AUC[r] - AUC[s]) / sqrt(S[r,r] + S[s,s] - 2*S[r,s])
    p_two_sided = 2 * (1 - Phi(|z|))

The paired covariance term ``S[r,s]`` is the load-bearing piece
that makes DeLong far more powerful than treating the two AUCs as
independent — exactly because the same eval pairs feed both
classifiers.
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
    _parse_yaml,
    _percentile,
    _ranks_with_ties,
    _detect_optional_packages,
    _round_floats,
    PreregistrationError,
    ReplicationError,
)
from coherence_engine.core.contradiction_direction import (
    fit_c_hat,
    project,
    cosine,
)
from coherence_engine.embeddings.utils import hoyer_sparsity


COMPARISON_SCHEMA_VERSION = "hoyer-vs-cosine-v1"

_HERE = Path(__file__).resolve().parent
DEFAULT_PREREGISTRATION_PATH = _HERE / "preregistration.yaml"
DEFAULT_FIXTURE_PATH = _HERE / "fixtures" / "tiny_pair_fixture.json"

_LABEL_POSITIVE = "contradiction"
_LABEL_NEGATIVE = "entailment"

_CLASSIFIERS = ("cosine", "hoyer", "projection")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ComparisonError(ReplicationError):
    """Base class for hoyer-vs-cosine harness failures."""


class InsufficientEvalSampleError(ComparisonError):
    def __init__(self, label: str, n: int, minimum: int):
        self.label = label
        self.n = int(n)
        self.minimum = int(minimum)
        super().__init__(
            f"INSUFFICIENT_SAMPLE: eval label={label!r} n={n} < "
            f"minimum={minimum}; refusing to emit report"
        )


# ---------------------------------------------------------------------------
# Pre-registration loading
# ---------------------------------------------------------------------------


def load_preregistration(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    p = Path(path) if path is not None else DEFAULT_PREREGISTRATION_PATH
    text = p.read_text(encoding="utf-8")
    parsed = _parse_yaml(text)
    required = (
        "version", "study_name", "dataset", "embedding_model",
        "primary_hypothesis", "bootstrap", "random_seed", "stopping_rule",
        "scoring", "cross_fit_protocol",
    )
    missing = [k for k in required if k not in parsed]
    if missing:
        raise PreregistrationError(
            f"preregistration is missing required keys: {missing}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Pair-corpus loading
# ---------------------------------------------------------------------------


def load_pair_corpus(path: os.PathLike) -> Dict[str, Any]:
    """Load a labeled pair-embedding corpus.

    Schema::

        {
          "schema": "hoyer-vs-cosine-fixture-v1",
          "model_id": ...,
          "model_version": ...,
          "dim": <int>,
          "pairs": [
             {"label": "contradiction" | "entailment",
              "u": [...], "v": [...]}
          ]
        }
    """
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    pairs_raw = payload.get("pairs")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise ComparisonError(
            f"{p}: expected non-empty 'pairs' list of {{label, u, v}} entries"
        )
    dim = payload.get("dim")
    if not isinstance(dim, int) or dim <= 0:
        raise ComparisonError(f"{p}: 'dim' must be a positive integer")
    grouped: Dict[str, List[Tuple[List[float], List[float]]]] = {
        _LABEL_POSITIVE: [],
        _LABEL_NEGATIVE: [],
    }
    for row in pairs_raw:
        label = row.get("label")
        if label not in grouped:
            # Pre-registration excludes neutral; silently skip anything
            # not in the binary task.
            continue
        u = row.get("u")
        v = row.get("v")
        if (
            not isinstance(u, list) or not isinstance(v, list)
            or len(u) != dim or len(v) != dim
        ):
            raise ComparisonError(
                f"{p}: each pair must have 'u' and 'v' lists of length dim={dim}"
            )
        grouped[label].append(
            ([float(x) for x in u], [float(x) for x in v])
        )
    return {
        "source": payload.get("source", "unknown"),
        "model_id": payload.get("model_id"),
        "model_version": payload.get("model_version"),
        "schema": payload.get("schema"),
        "dim": dim,
        "pairs": grouped,
    }


# ---------------------------------------------------------------------------
# Statistics — ROC AUC + DeLong
# ---------------------------------------------------------------------------


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """ROC AUC via the rank-sum formula with average-rank tie correction.

    ``labels`` are 1 for positive and 0 for negative. Returns 0.5 when
    either class is empty so canonical JSON stays comparable across
    runs (rather than emitting NaN).
    """
    if len(scores) != len(labels):
        raise ValueError("roc_auc: scores and labels length mismatch")
    pos_idx = [i for i, y in enumerate(labels) if y == 1]
    neg_idx = [i for i, y in enumerate(labels) if y == 0]
    if not pos_idx or not neg_idx:
        return 0.5
    ranks = _ranks_with_ties(list(scores))
    rank_sum_pos = sum(ranks[i] for i in pos_idx)
    u = rank_sum_pos - len(pos_idx) * (len(pos_idx) + 1) / 2.0
    return float(u / (len(pos_idx) * len(neg_idx)))


def _psi_matrix(pos_scores: Sequence[float], neg_scores: Sequence[float]) -> np.ndarray:
    """psi(x, y) = 1 if x > y, 0.5 if equal, 0 if x < y; shape (n_pos, n_neg)."""
    x = np.asarray(pos_scores, dtype=np.float64).reshape(-1, 1)
    y = np.asarray(neg_scores, dtype=np.float64).reshape(1, -1)
    psi = np.where(x > y, 1.0, np.where(x == y, 0.5, 0.0))
    return psi


def delong_components(
    pos_scores: Sequence[float],
    neg_scores: Sequence[float],
) -> Tuple[float, np.ndarray, np.ndarray]:
    """Return ``(AUC, V10, V01)`` for one classifier — DeLong placement values."""
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    if n_pos == 0 or n_neg == 0:
        return 0.5, np.zeros(n_pos), np.zeros(n_neg)
    psi = _psi_matrix(pos_scores, neg_scores)
    v10 = psi.mean(axis=1)              # length n_pos
    v01 = psi.mean(axis=0)              # length n_neg
    auc = float(v10.mean())
    return auc, v10, v01


def _cov_matrix(rows: Sequence[np.ndarray]) -> np.ndarray:
    """Sample covariance matrix across rows (each row is one classifier)."""
    stacked = np.vstack([np.asarray(r, dtype=np.float64) for r in rows])
    n = stacked.shape[1]
    if n < 2:
        return np.zeros((stacked.shape[0], stacked.shape[0]))
    means = stacked.mean(axis=1, keepdims=True)
    centered = stacked - means
    return (centered @ centered.T) / (n - 1)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def delong_test(
    pos_scores_a: Sequence[float],
    neg_scores_a: Sequence[float],
    pos_scores_b: Sequence[float],
    neg_scores_b: Sequence[float],
) -> Dict[str, float]:
    """Two-sided DeLong z-test for AUC[a] == AUC[b].

    The two classifiers must be evaluated on the SAME ordered set of
    positive and negative pairs — that pairing is what the covariance
    term captures and is the load-bearing assumption of the test.
    """
    n_pos = len(pos_scores_a)
    n_neg = len(neg_scores_a)
    if (
        n_pos != len(pos_scores_b)
        or n_neg != len(neg_scores_b)
    ):
        raise ValueError(
            "delong_test: both classifiers must score the same eval set"
        )
    auc_a, v10_a, v01_a = delong_components(pos_scores_a, neg_scores_a)
    auc_b, v10_b, v01_b = delong_components(pos_scores_b, neg_scores_b)
    s10 = _cov_matrix([v10_a, v10_b])
    s01 = _cov_matrix([v01_a, v01_b])
    if n_pos == 0 or n_neg == 0:
        var_diff = 0.0
    else:
        s = s10 / n_pos + s01 / n_neg
        var_diff = float(s[0, 0] + s[1, 1] - 2.0 * s[0, 1])
    auc_diff = auc_a - auc_b
    if var_diff <= 0.0:
        # Degenerate: identical scoring or zero variance. Report z=0
        # and p=1 so the canonical JSON stays well-defined; downstream
        # code treats ``var_diff <= 0`` as "no power, do not reject".
        return {
            "auc_a": auc_a,
            "auc_b": auc_b,
            "auc_diff": auc_diff,
            "var_diff": var_diff,
            "z": 0.0,
            "p_value": 1.0,
        }
    z = auc_diff / math.sqrt(var_diff)
    p = 2.0 * (1.0 - _normal_cdf(abs(z)))
    return {
        "auc_a": auc_a,
        "auc_b": auc_b,
        "auc_diff": auc_diff,
        "var_diff": var_diff,
        "z": z,
        "p_value": p,
    }


def bootstrap_paired_auc_ci(
    pos_scores_per_cls: Mapping[str, Sequence[float]],
    neg_scores_per_cls: Mapping[str, Sequence[float]],
    *,
    iterations: int,
    seed: int,
    ci: float = 95.0,
) -> Dict[str, Dict[str, float]]:
    """Paired bootstrap CI: one set of resampled indices, three AUCs.

    Returns ``{cls: {ci_low, ci_high, mean, std}}`` for each classifier.
    """
    classifier_names = list(pos_scores_per_cls.keys())
    n_pos = len(next(iter(pos_scores_per_cls.values())))
    n_neg = len(next(iter(neg_scores_per_cls.values())))
    rng = random.Random(seed)
    samples: Dict[str, List[float]] = {c: [] for c in classifier_names}
    for _ in range(iterations):
        idx_pos = [rng.randrange(n_pos) for _ in range(n_pos)]
        idx_neg = [rng.randrange(n_neg) for _ in range(n_neg)]
        labels = [1] * n_pos + [0] * n_neg
        for c in classifier_names:
            ps = pos_scores_per_cls[c]
            ns = neg_scores_per_cls[c]
            scores = [ps[i] for i in idx_pos] + [ns[j] for j in idx_neg]
            samples[c].append(roc_auc(scores, labels))
    out: Dict[str, Dict[str, float]] = {}
    alpha_pct = (100.0 - ci) / 2.0
    for c, vals in samples.items():
        if not vals:
            out[c] = {
                "ci_low": 0.5, "ci_high": 0.5,
                "mean": 0.5, "std": 0.0,
                "ci_percent": ci,
                "n_bootstrap_iterations": iterations,
            }
            continue
        sorted_vals = sorted(vals)
        mean = sum(vals) / len(vals)
        var = sum((x - mean) ** 2 for x in vals) / max(1, len(vals) - 1)
        out[c] = {
            "ci_low": _percentile(sorted_vals, alpha_pct),
            "ci_high": _percentile(sorted_vals, 100.0 - alpha_pct),
            "mean": mean,
            "std": math.sqrt(var),
            "ci_percent": ci,
            "n_bootstrap_iterations": iterations,
        }
    return out


# ---------------------------------------------------------------------------
# Configuration + report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonConfig:
    seed: int = 49
    n_bootstrap_iterations: int = 10_000
    alpha: float = 0.01
    ci_percent: float = 95.0
    fixture_path: Optional[str] = None
    corpus_path: Optional[str] = None
    preregistration_path: Optional[str] = None
    minimum_eval_pairs_override: Optional[int] = None


@dataclass
class ComparisonReport:
    schema_version: str
    run_id: str
    config: Dict[str, Any]
    preregistration: Dict[str, Any]
    inputs: Dict[str, Any]
    auc: Dict[str, Any]
    delong: Dict[str, Any]
    interpretation: Dict[str, Any]
    generated_with: Dict[str, Any]

    def to_canonical_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "config": self.config,
            "preregistration": self.preregistration,
            "inputs": self.inputs,
            "auc": self.auc,
            "delong": self.delong,
            "interpretation": self.interpretation,
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


def _digest_pairs(pairs: Mapping[str, Sequence[Tuple[Sequence[float], Sequence[float]]]]) -> str:
    h = hashlib.sha256()
    for label in (_LABEL_POSITIVE, _LABEL_NEGATIVE):
        h.update(label.encode("ascii"))
        h.update(b":")
        for u, v in pairs.get(label, []):
            for x in u:
                h.update(f"{x:.10f}".encode("ascii"))
                h.update(b",")
            h.update(b"|")
            for x in v:
                h.update(f"{x:.10f}".encode("ascii"))
                h.update(b",")
            h.update(b";")
        h.update(b"||")
    return h.hexdigest()


def _split_50_50(
    n: int, *, seed: int
) -> Tuple[List[int], List[int]]:
    """Deterministic 50/50 split of indices ``[0, n)``."""
    rng = random.Random(seed)
    idx = list(range(n))
    rng.shuffle(idx)
    half = n // 2
    return idx[:half], idx[half:]


def _resolve_minimum_eval(
    prereg: Mapping[str, Any], override: Optional[int]
) -> int:
    if override is not None:
        return int(override)
    rule = prereg.get("stopping_rule") or {}
    return int(rule.get("minimum_eval_pairs_per_label", 200))


def _score_pair_cosine(u: Sequence[float], v: Sequence[float]) -> float:
    return 1.0 - cosine(u, v)


def _score_pair_hoyer(u: Sequence[float], v: Sequence[float]) -> float:
    diff = [a - b for a, b in zip(u, v)]
    return float(hoyer_sparsity(diff))


def _score_pair_projection(
    u: Sequence[float], v: Sequence[float], c_hat: np.ndarray
) -> float:
    arr = np.asarray([[u, v]], dtype=np.float64)
    return float(project(arr, c_hat)[0])


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_comparison(config: ComparisonConfig) -> ComparisonReport:
    prereg = load_preregistration(config.preregistration_path)
    seed = int(config.seed)

    if config.fixture_path:
        corpus = load_pair_corpus(config.fixture_path)
        corpus_source = "fixture"
    elif config.corpus_path:
        corpus = load_pair_corpus(config.corpus_path)
        corpus_source = corpus.get("source") or "corpus_json"
    else:
        raise ComparisonError(
            "ComparisonConfig: must provide one of fixture_path or corpus_path"
        )

    pairs: Dict[str, List[Tuple[List[float], List[float]]]] = corpus["pairs"]
    pos_pairs = pairs[_LABEL_POSITIVE]
    neg_pairs = pairs[_LABEL_NEGATIVE]
    if not pos_pairs or not neg_pairs:
        raise ComparisonError(
            "corpus must contain at least one contradiction AND one "
            "entailment pair"
        )

    # ── 1. Deterministic 50/50 split ────────────────────────────────
    pos_fit_idx, pos_eval_idx = _split_50_50(len(pos_pairs), seed=seed)
    neg_fit_idx, neg_eval_idx = _split_50_50(len(neg_pairs), seed=seed + 1)
    if not pos_fit_idx or not pos_eval_idx:
        # Tiny inputs — fall back to using the same single pair on both
        # sides so the harness can still run on the smallest fixtures.
        pos_fit_idx = pos_eval_idx = list(range(len(pos_pairs)))
    if not neg_eval_idx:
        neg_eval_idx = list(range(len(neg_pairs)))

    minimum_eval = _resolve_minimum_eval(prereg, config.minimum_eval_pairs_override)
    if corpus_source != "fixture":
        if len(pos_eval_idx) < minimum_eval:
            raise InsufficientEvalSampleError(
                _LABEL_POSITIVE, len(pos_eval_idx), minimum_eval
            )
        if len(neg_eval_idx) < minimum_eval:
            raise InsufficientEvalSampleError(
                _LABEL_NEGATIVE, len(neg_eval_idx), minimum_eval
            )

    # ── 2. Fit ĉ on the fit-half of contradiction pairs ────────────
    fit_arr = np.array(
        [[pos_pairs[i][0], pos_pairs[i][1]] for i in pos_fit_idx],
        dtype=np.float64,
    )
    c_hat = fit_c_hat(fit_arr)

    # ── 3. Score the eval set with all three classifiers ───────────
    pos_eval = [pos_pairs[i] for i in pos_eval_idx]
    neg_eval = [neg_pairs[j] for j in neg_eval_idx]

    pos_scores = {c: [] for c in _CLASSIFIERS}
    neg_scores = {c: [] for c in _CLASSIFIERS}
    for u, v in pos_eval:
        pos_scores["cosine"].append(_score_pair_cosine(u, v))
        pos_scores["hoyer"].append(_score_pair_hoyer(u, v))
        pos_scores["projection"].append(_score_pair_projection(u, v, c_hat))
    for u, v in neg_eval:
        neg_scores["cosine"].append(_score_pair_cosine(u, v))
        neg_scores["hoyer"].append(_score_pair_hoyer(u, v))
        neg_scores["projection"].append(_score_pair_projection(u, v, c_hat))

    # ── 4. Point AUCs ──────────────────────────────────────────────
    auc_point: Dict[str, float] = {}
    for c in _CLASSIFIERS:
        scores = pos_scores[c] + neg_scores[c]
        labels = [1] * len(pos_scores[c]) + [0] * len(neg_scores[c])
        auc_point[c] = roc_auc(scores, labels)

    # ── 5. Paired bootstrap CIs ────────────────────────────────────
    boot = bootstrap_paired_auc_ci(
        pos_scores, neg_scores,
        iterations=int(config.n_bootstrap_iterations),
        seed=seed + 7919,
        ci=float(config.ci_percent),
    )
    auc_block: Dict[str, Any] = {}
    for c in _CLASSIFIERS:
        auc_block[c] = {
            "auc": auc_point[c],
            "ci_low": boot[c]["ci_low"],
            "ci_high": boot[c]["ci_high"],
            "ci_mean": boot[c]["mean"],
            "ci_std": boot[c]["std"],
            "ci_percent": boot[c]["ci_percent"],
            "n_bootstrap_iterations": boot[c]["n_bootstrap_iterations"],
            "n_eval_positive": len(pos_scores[c]),
            "n_eval_negative": len(neg_scores[c]),
        }

    # ── 6. DeLong tests ────────────────────────────────────────────
    delong_block: Dict[str, Any] = {}
    for cls_other in ("hoyer", "projection"):
        result = delong_test(
            pos_scores[cls_other], neg_scores[cls_other],
            pos_scores["cosine"], neg_scores["cosine"],
        )
        # Test orientation: a == cls_other, b == cosine. Difference is
        # AUC(other) - AUC(cosine), so a positive sign means "other wins".
        result["alpha"] = float(config.alpha)
        result["reject_null"] = bool(result["p_value"] < float(config.alpha))
        result["test"] = "DeLong two-sided z-test for AUC equality"
        result["null_hypothesis"] = (
            f"AUC({cls_other}) == AUC(cosine)"
        )
        result["winner"] = (
            cls_other if result["auc_diff"] > 0 else "cosine"
        ) if result["reject_null"] else "no_difference"
        delong_block[f"{cls_other}_vs_cosine"] = result

    # ── 7. Interpretation ──────────────────────────────────────────
    primary = delong_block["hoyer_vs_cosine"]
    interp = {
        "primary_outcome": (
            "hoyer_signal_differs_from_cosine"
            if primary["reject_null"]
            else "no_evidence_hoyer_differs_from_cosine"
        ),
        "primary_winner": primary["winner"],
        "primary_p_value": primary["p_value"],
        "secondary_projection_vs_cosine_outcome": (
            "projection_signal_differs_from_cosine"
            if delong_block["projection_vs_cosine"]["reject_null"]
            else "no_evidence_projection_differs_from_cosine"
        ),
        "secondary_winner": delong_block["projection_vs_cosine"]["winner"],
        "alpha": float(config.alpha),
        "criterion": (
            "DeLong two-sided p-value < alpha rejects equality of AUCs"
        ),
        "directionality": (
            "two-sided per preregistration; direction read off auc_diff sign"
        ),
    }

    # ── 8. Inputs / config / preregistration ───────────────────────
    inputs_digest = _digest_pairs(pairs)
    inputs = {
        "corpus_source": corpus_source,
        "corpus_digest_sha256": inputs_digest,
        "model_id": corpus.get("model_id"),
        "model_version": corpus.get("model_version"),
        "dim": int(corpus["dim"]),
        "n_pairs_total": {
            _LABEL_POSITIVE: len(pos_pairs),
            _LABEL_NEGATIVE: len(neg_pairs),
        },
        "n_fit_pairs": {
            _LABEL_POSITIVE: len(pos_fit_idx),
            _LABEL_NEGATIVE: len(neg_fit_idx),
        },
        "n_eval_pairs": {
            _LABEL_POSITIVE: len(pos_eval_idx),
            _LABEL_NEGATIVE: len(neg_eval_idx),
        },
        "c_hat_norm": float(np.linalg.norm(c_hat)),
        "minimum_eval_pairs_per_label": minimum_eval,
    }

    config_for_report = {
        "seed": seed,
        "n_bootstrap_iterations": int(config.n_bootstrap_iterations),
        "alpha": float(config.alpha),
        "ci_percent": float(config.ci_percent),
        "corpus_source": corpus_source,
    }

    run_id = hashlib.sha256(
        f"{inputs_digest}|seed={seed}|"
        f"boot={config.n_bootstrap_iterations}|"
        f"alpha={config.alpha}".encode("ascii")
    ).hexdigest()[:16]

    report = ComparisonReport(
        schema_version=COMPARISON_SCHEMA_VERSION,
        run_id=run_id,
        config=config_for_report,
        preregistration={
            "version": prereg.get("version"),
            "study_name": prereg.get("study_name"),
            "primary_hypothesis_id": (
                (prereg.get("primary_hypothesis") or {}).get("id")
            ),
            "alpha": (prereg.get("primary_hypothesis") or {}).get("alpha"),
            "n_bootstrap": (
                (prereg.get("primary_hypothesis") or {}).get("n_bootstrap")
            ),
            "test": (prereg.get("primary_hypothesis") or {}).get("test"),
        },
        inputs=inputs,
        auc=_round_floats(auc_block),
        delong=_round_floats(delong_block),
        interpretation=_round_floats(interp),
        generated_with=_detect_optional_packages(),
    )
    return report


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hoyer-vs-cosine",
        description=(
            "Hoyer-sparsity vs raw-cosine head-to-head ROC harness "
            "(prompt 49, Wave 13)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on the bundled tiny fixture and emit JSON to stdout.",
    )
    parser.add_argument(
        "--corpus",
        dest="corpus_path",
        type=str,
        default=None,
        help="Path to a labeled pair-embedding JSON.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write the canonical JSON report here (default: stdout).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override the pre-registered random seed.",
    )
    parser.add_argument(
        "--n-bootstrap-iterations",
        type=int,
        default=None,
        help="Override the bootstrap iteration count.",
    )
    parser.add_argument(
        "--preregistration",
        type=str,
        default=None,
        help="Override path to preregistration.yaml.",
    )
    parser.add_argument(
        "--minimum-eval-pairs-per-label",
        type=int,
        default=None,
        help="Override stopping_rule.minimum_eval_pairs_per_label (testing only).",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> ComparisonConfig:
    prereg = load_preregistration(args.preregistration)
    pri = prereg.get("primary_hypothesis") or {}
    boot = prereg.get("bootstrap") or {}
    seed = (
        args.seed if args.seed is not None
        else int(prereg.get("random_seed") or 49)
    )
    n_boot = (
        args.n_bootstrap_iterations
        if args.n_bootstrap_iterations is not None
        else int(boot.get("iterations") or 10_000)
    )
    if args.dry_run:
        return ComparisonConfig(
            seed=seed,
            n_bootstrap_iterations=args.n_bootstrap_iterations or 200,
            alpha=float(pri.get("alpha") or 0.01),
            ci_percent=float(pri.get("ci_percent") or 95.0),
            fixture_path=str(DEFAULT_FIXTURE_PATH),
            preregistration_path=args.preregistration,
            minimum_eval_pairs_override=(
                args.minimum_eval_pairs_per_label or 4
            ),
        )
    return ComparisonConfig(
        seed=seed,
        n_bootstrap_iterations=n_boot,
        alpha=float(pri.get("alpha") or 0.01),
        ci_percent=float(pri.get("ci_percent") or 95.0),
        corpus_path=args.corpus_path,
        preregistration_path=args.preregistration,
        minimum_eval_pairs_override=args.minimum_eval_pairs_per_label,
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
        report = run_comparison(config)
    except InsufficientEvalSampleError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 3
    except ComparisonError as exc:
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
