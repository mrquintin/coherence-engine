"""Contradiction-direction (ĉ) cross-domain stability study.

Prompt 48, Wave 13. Depends on prompt 47's replication-harness pattern.

Pipeline:

  1. Load a labeled per-domain dataset of contradiction / entailment
     pairs (each pair is two sentence embeddings of identical
     dimensionality).
  2. For each domain, fit ĉ on that domain's contradiction pairs using
     the production routine in ``core.contradiction_direction.fit_c_hat``.
  3. Report the pairwise abs-cosine matrix across the per-domain ĉ
     vectors (sign-invariant, since ĉ is only defined up to sign).
  4. Cross-domain ROC: for every ordered pair of domains (A, B) with
     B ≠ A, score B's labeled pairs by ``|⟨u-v, ĉ_A⟩|`` and report the
     ROC AUC of contradiction (positive) vs entailment (negative). The
     within-domain baseline uses a deterministic 50/50 split — fit on
     the first half, evaluate on the second.
  5. Subsample-size sensitivity: from the full pooled contradiction
     set, draw ``n_subsamples`` random subsets at each pre-registered
     size N (default {200, 500, 1000}), fit ĉ on each, and measure
     abs-cosine to ĉ(full_pool). Report mean / std / 95% bootstrap CI.
  6. Emit a deterministic JSON report whose canonical bytes are stable
     across runs given the same seed and the same input pairs.

Determinism guarantees mirror the cosine-paradox harness: the YAML
parser is the tiny stdlib subset used there, all index sampling uses
``random.Random`` seeded from the pre-registration, and bootstrap CI
endpoints are reported to 1e-10 precision after a final rounding pass.

Falsification thresholds (from preregistration.yaml):

  * pairwise abs-cosine across domains ≥ 0.70  AND
  * median cross-domain AUC drop vs same-domain baseline ≤ 0.05
  → recommend a single global ĉ.

Otherwise: recommend per-domain ĉ. Both outcomes are publishable.
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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

# We deliberately reuse the cosine-paradox harness primitives where
# they apply (YAML parsing, ranks-with-ties, percentile, optional-
# package detection) so the two studies share one tested surface and
# emit reports under the same deterministic contract.
from coherence_engine.Experiments.Cosine_Paradox_Replication.run_replication import (
    _parse_yaml,
    _ranks_with_ties,
    _percentile,
    _detect_optional_packages,
    _round_floats,
    PreregistrationError,
    ReplicationError,
)
from coherence_engine.core.contradiction_direction import (
    fit_c_hat,
    project,
    abs_cosine,
)


STABILITY_SCHEMA_VERSION = "c-hat-stability-v1"

_HERE = Path(__file__).resolve().parent
DEFAULT_PREREGISTRATION_PATH = _HERE / "preregistration.yaml"
DEFAULT_FIXTURE_PATH = _HERE / "fixtures" / "tiny_two_domain_fixture.json"

_LABEL_POSITIVE = "contradiction"
_LABEL_NEGATIVE = "entailment"

_FALSIFICATION_PAIRWISE_COSINE = 0.70
_FALSIFICATION_AUC_DROP = 0.05


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StabilityError(ReplicationError):
    """Base class for ĉ-stability harness failures."""


class InsufficientDomainSampleError(StabilityError):
    def __init__(self, domain: str, label: str, n: int, minimum: int):
        self.domain = domain
        self.label = label
        self.n = int(n)
        self.minimum = int(minimum)
        super().__init__(
            f"INSUFFICIENT_SAMPLE: domain={domain!r} label={label!r} "
            f"n={n} < minimum={minimum}; refusing to emit report"
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
        "primary_hypothesis", "subsample_sizes", "n_subsamples",
        "bootstrap", "seeds", "stopping_rule",
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
    """Load a JSON file with per-domain labeled pair embeddings.

    Schema::

        {
          "schema": "c-hat-stability-fixture-v1",
          "model_id": ...,
          "model_version": ...,
          "dim": <int>,
          "domains": {
             "<domain_name>": {
                "contradiction": [[[u...], [v...]], ...],
                "entailment":    [[[u...], [v...]], ...]
             }, ...
          }
        }
    """
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    domains = payload.get("domains")
    if not isinstance(domains, dict) or not domains:
        raise StabilityError(
            f"{p}: expected non-empty 'domains' object keyed by domain name"
        )
    dim = payload.get("dim")
    if not isinstance(dim, int) or dim <= 0:
        raise StabilityError(f"{p}: 'dim' must be a positive integer")
    parsed: Dict[str, Dict[str, np.ndarray]] = {}
    for domain_name, labels in domains.items():
        if not isinstance(labels, dict):
            raise StabilityError(
                f"{p}: domain {domain_name!r} must map labels to pair lists"
            )
        slot: Dict[str, np.ndarray] = {}
        for label in (_LABEL_POSITIVE, _LABEL_NEGATIVE):
            rows = labels.get(label, [])
            if not isinstance(rows, list):
                raise StabilityError(
                    f"{p}: domain {domain_name!r} label {label!r} must be a list"
                )
            if not rows:
                slot[label] = np.zeros((0, 2, dim), dtype=np.float64)
                continue
            arr = np.asarray(rows, dtype=np.float64)
            if arr.ndim != 3 or arr.shape[1:] != (2, dim):
                raise StabilityError(
                    f"{p}: domain {domain_name!r} label {label!r} pairs must "
                    f"have shape (n, 2, {dim}); got {arr.shape}"
                )
            slot[label] = arr
        parsed[domain_name] = slot
    return {
        "source": payload.get("source", "unknown"),
        "model_id": payload.get("model_id"),
        "model_version": payload.get("model_version"),
        "schema": payload.get("schema"),
        "dim": dim,
        "domains": parsed,
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """ROC AUC via the rank-sum formula (handles ties via average ranks).

    ``labels`` are 1 for positive and 0 for negative.
    """
    if len(scores) != len(labels):
        raise ValueError("roc_auc: scores and labels length mismatch")
    pos = [i for i, y in enumerate(labels) if y == 1]
    neg = [i for i, y in enumerate(labels) if y == 0]
    n_pos = len(pos)
    n_neg = len(neg)
    if n_pos == 0 or n_neg == 0:
        # Undefined; report 0.5 as the neutral value rather than NaN so the
        # canonical JSON stays comparable across runs.
        return 0.5
    ranks = _ranks_with_ties(list(scores))
    rank_sum_pos = sum(ranks[i] for i in pos)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return float(u / (n_pos * n_neg))


def bootstrap_ci(
    samples: Sequence[float], *, ci: float = 95.0
) -> Tuple[float, float, float, float]:
    """Bootstrap CI given pre-computed bootstrap statistic samples.

    Returns ``(low, high, mean, std)``.
    """
    if not samples:
        return 0.0, 0.0, 0.0, 0.0
    sorted_s = sorted(samples)
    alpha = (100.0 - ci) / 2.0
    low = _percentile(sorted_s, alpha)
    high = _percentile(sorted_s, 100.0 - alpha)
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / max(1, len(samples) - 1)
    std = math.sqrt(var)
    return low, high, mean, std


def bootstrap_auc_ci(
    pairs_pos: np.ndarray,
    pairs_neg: np.ndarray,
    c_hat: np.ndarray,
    *,
    iterations: int,
    seed: int,
    ci: float = 95.0,
) -> Tuple[float, float, float, float]:
    """Bootstrap CI for AUC of ⟨u-v, ĉ⟩ separating contradictions from entailments."""
    if pairs_pos.shape[0] == 0 or pairs_neg.shape[0] == 0:
        return 0.5, 0.5, 0.5, 0.0
    pos_scores = project(pairs_pos, c_hat).tolist()
    neg_scores = project(pairs_neg, c_hat).tolist()
    rng = random.Random(seed)
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    samples: List[float] = []
    for _ in range(iterations):
        rs_pos = [pos_scores[rng.randrange(n_pos)] for _ in range(n_pos)]
        rs_neg = [neg_scores[rng.randrange(n_neg)] for _ in range(n_neg)]
        scores = rs_pos + rs_neg
        labels = [1] * n_pos + [0] * n_neg
        samples.append(roc_auc(scores, labels))
    return bootstrap_ci(samples, ci=ci)


def bootstrap_cosine_ci(
    pairs_a: np.ndarray,
    pairs_b: np.ndarray,
    *,
    iterations: int,
    seed: int,
    ci: float = 95.0,
) -> Tuple[float, float, float, float]:
    """Bootstrap CI for abs-cosine of ĉ_A and ĉ_B by resampling the fit pairs."""
    if pairs_a.shape[0] < 2 or pairs_b.shape[0] < 2:
        return 0.0, 0.0, 0.0, 0.0
    rng = random.Random(seed)
    samples: List[float] = []
    for _ in range(iterations):
        idx_a = [rng.randrange(pairs_a.shape[0]) for _ in range(pairs_a.shape[0])]
        idx_b = [rng.randrange(pairs_b.shape[0]) for _ in range(pairs_b.shape[0])]
        try:
            ca = fit_c_hat(pairs_a[idx_a])
            cb = fit_c_hat(pairs_b[idx_b])
        except ValueError:
            continue
        samples.append(abs_cosine(ca, cb))
    return bootstrap_ci(samples, ci=ci)


# ---------------------------------------------------------------------------
# Configuration + report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StabilityConfig:
    seed: int = 4747
    n_bootstrap_iterations: int = 1000
    ci_percent: float = 95.0
    n_subsamples: int = 50
    subsample_sizes: Tuple[int, ...] = (200, 500, 1000)
    fixture_path: Optional[str] = None
    corpus_path: Optional[str] = None
    preregistration_path: Optional[str] = None
    minimum_pairs_override: Optional[int] = None


@dataclass
class StabilityReport:
    schema_version: str
    run_id: str
    config: Dict[str, Any]
    preregistration: Dict[str, Any]
    inputs: Dict[str, Any]
    per_domain_c_hat: Dict[str, Any]
    pairwise_cosine: Dict[str, Any]
    cross_domain_auc: Dict[str, Any]
    subsample_sensitivity: Dict[str, Any]
    decision: Dict[str, Any]
    generated_with: Dict[str, Any]

    def to_canonical_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "config": self.config,
            "preregistration": self.preregistration,
            "inputs": self.inputs,
            "per_domain_c_hat": self.per_domain_c_hat,
            "pairwise_cosine": self.pairwise_cosine,
            "cross_domain_auc": self.cross_domain_auc,
            "subsample_sensitivity": self.subsample_sensitivity,
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


def _digest_corpus(domains: Mapping[str, Mapping[str, np.ndarray]]) -> str:
    h = hashlib.sha256()
    for domain in sorted(domains.keys()):
        h.update(domain.encode("ascii"))
        h.update(b"|")
        for label in (_LABEL_POSITIVE, _LABEL_NEGATIVE):
            h.update(label.encode("ascii"))
            h.update(b":")
            arr = domains[domain].get(label)
            if arr is None:
                continue
            for v in arr.ravel().tolist():
                h.update(f"{v:.10f}".encode("ascii"))
                h.update(b",")
            h.update(b"|")
    return h.hexdigest()


def _split_half(
    pairs: np.ndarray, *, seed: int
) -> Tuple[np.ndarray, np.ndarray]:
    """Deterministic 50/50 split for the within-domain AUC baseline."""
    n = pairs.shape[0]
    if n < 2:
        return pairs, pairs
    rng = random.Random(seed)
    idx = list(range(n))
    rng.shuffle(idx)
    half = n // 2
    fit_idx = idx[:half]
    eval_idx = idx[half:]
    return pairs[fit_idx], pairs[eval_idx]


def _resolve_minimum_pairs(
    prereg: Mapping[str, Any], override: Optional[int]
) -> int:
    if override is not None:
        return int(override)
    rule = prereg.get("stopping_rule") or {}
    return int(rule.get("minimum_pairs_per_domain_label", 200))


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_stability_study(config: StabilityConfig) -> StabilityReport:
    prereg = load_preregistration(config.preregistration_path)
    seeds = prereg.get("seeds") or {}

    if config.fixture_path:
        corpus = load_pair_corpus(config.fixture_path)
        corpus_source = "fixture"
    elif config.corpus_path:
        corpus = load_pair_corpus(config.corpus_path)
        corpus_source = corpus.get("source") or "corpus_json"
    else:
        raise StabilityError(
            "StabilityConfig: must provide one of fixture_path or corpus_path"
        )

    domains: Dict[str, Dict[str, np.ndarray]] = corpus["domains"]
    minimum_pairs = _resolve_minimum_pairs(prereg, config.minimum_pairs_override)

    # Stopping rule (relaxed for fixtures so dry-run + unit tests work).
    if corpus_source != "fixture":
        for d, labels in domains.items():
            for label in (_LABEL_POSITIVE, _LABEL_NEGATIVE):
                arr = labels.get(label)
                n = 0 if arr is None else int(arr.shape[0])
                if n < minimum_pairs:
                    raise InsufficientDomainSampleError(
                        d, label, n, minimum_pairs
                    )

    # ── 1. Per-domain ĉ ────────────────────────────────────────────
    domain_names = sorted(domains.keys())
    c_hats: Dict[str, np.ndarray] = {}
    per_domain_summary: Dict[str, Dict[str, Any]] = {}
    for d in domain_names:
        pos = domains[d][_LABEL_POSITIVE]
        if pos.shape[0] < 1:
            raise StabilityError(
                f"domain {d!r} has zero contradiction pairs; cannot fit ĉ"
            )
        c = fit_c_hat(pos)
        c_hats[d] = c
        per_domain_summary[d] = {
            "n_contradiction_pairs": int(pos.shape[0]),
            "n_entailment_pairs": int(domains[d][_LABEL_NEGATIVE].shape[0]),
            "c_hat_norm": float(np.linalg.norm(c)),
            "c_hat": [float(x) for x in c.tolist()],
        }

    # ── 2. Pairwise abs-cosine matrix ──────────────────────────────
    pairwise: Dict[str, Dict[str, Any]] = {}
    boot_seed_master = int(
        seeds.get("bootstrap_master") or (config.seed + 4)
    )
    for i, a in enumerate(domain_names):
        pairwise[a] = {}
        for j, b in enumerate(domain_names):
            point = abs_cosine(c_hats[a], c_hats[b])
            if a == b:
                pairwise[a][b] = {
                    "abs_cosine": point,
                    "ci_low": point,
                    "ci_high": point,
                    "ci_std": 0.0,
                    "ci_percent": config.ci_percent,
                    "n_bootstrap_iterations": 0,
                }
                continue
            ci_low, ci_high, _mean, ci_std = bootstrap_cosine_ci(
                domains[a][_LABEL_POSITIVE],
                domains[b][_LABEL_POSITIVE],
                iterations=config.n_bootstrap_iterations,
                seed=boot_seed_master + 1000 * i + j,
                ci=config.ci_percent,
            )
            pairwise[a][b] = {
                "abs_cosine": point,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "ci_std": ci_std,
                "ci_percent": config.ci_percent,
                "n_bootstrap_iterations": config.n_bootstrap_iterations,
            }

    pairwise_offdiag = [
        pairwise[a][b]["abs_cosine"]
        for a in domain_names for b in domain_names if a != b
    ]
    pairwise_summary = {
        "min": min(pairwise_offdiag) if pairwise_offdiag else 0.0,
        "max": max(pairwise_offdiag) if pairwise_offdiag else 0.0,
        "mean": (
            sum(pairwise_offdiag) / len(pairwise_offdiag)
            if pairwise_offdiag else 0.0
        ),
        "median": (
            sorted(pairwise_offdiag)[len(pairwise_offdiag) // 2]
            if pairwise_offdiag else 0.0
        ),
        "n_pairs": len(pairwise_offdiag),
    }

    # ── 3. Cross-domain AUC (with within-domain baseline) ──────────
    fit_seed = int(seeds.get("per_domain_fit") or (config.seed + 1))
    eval_seed = int(seeds.get("cross_domain_eval") or (config.seed + 2))
    within: Dict[str, Dict[str, Any]] = {}
    cross: Dict[str, Dict[str, Any]] = {}

    for idx, a in enumerate(domain_names):
        # Within-domain baseline: fit on half, evaluate on the other half.
        pos_a = domains[a][_LABEL_POSITIVE]
        neg_a = domains[a][_LABEL_NEGATIVE]
        fit_a, eval_pos_a = _split_half(pos_a, seed=fit_seed + idx)
        try:
            c_within = fit_c_hat(fit_a) if fit_a.shape[0] >= 1 else c_hats[a]
        except ValueError:
            c_within = c_hats[a]
        scores_pos = project(eval_pos_a, c_within).tolist()
        scores_neg = project(neg_a, c_within).tolist()
        baseline_auc = roc_auc(
            scores_pos + scores_neg,
            [1] * len(scores_pos) + [0] * len(scores_neg),
        )
        ci_low, ci_high, _m, ci_std = bootstrap_auc_ci(
            eval_pos_a, neg_a, c_within,
            iterations=config.n_bootstrap_iterations,
            seed=boot_seed_master + 7919 * (idx + 1),
            ci=config.ci_percent,
        )
        within[a] = {
            "auc": baseline_auc,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "ci_std": ci_std,
            "ci_percent": config.ci_percent,
            "n_bootstrap_iterations": config.n_bootstrap_iterations,
            "n_eval_positive": int(eval_pos_a.shape[0]),
            "n_eval_negative": int(neg_a.shape[0]),
        }

    for ia, a in enumerate(domain_names):
        cross[a] = {}
        for ib, b in enumerate(domain_names):
            if a == b:
                cross[a][b] = within[a]
                continue
            pos_b = domains[b][_LABEL_POSITIVE]
            neg_b = domains[b][_LABEL_NEGATIVE]
            scores_pos = project(pos_b, c_hats[a]).tolist()
            scores_neg = project(neg_b, c_hats[a]).tolist()
            point = roc_auc(
                scores_pos + scores_neg,
                [1] * len(scores_pos) + [0] * len(scores_neg),
            )
            ci_low, ci_high, _m, ci_std = bootstrap_auc_ci(
                pos_b, neg_b, c_hats[a],
                iterations=config.n_bootstrap_iterations,
                seed=eval_seed + 100 * ia + ib,
                ci=config.ci_percent,
            )
            cross[a][b] = {
                "auc": point,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "ci_std": ci_std,
                "ci_percent": config.ci_percent,
                "n_bootstrap_iterations": config.n_bootstrap_iterations,
                "n_eval_positive": int(pos_b.shape[0]),
                "n_eval_negative": int(neg_b.shape[0]),
                "auc_drop_vs_baseline": within[b]["auc"] - point,
            }

    drops = [
        cross[a][b]["auc_drop_vs_baseline"]
        for a in domain_names for b in domain_names if a != b
    ]
    cross_summary = {
        "n_cross_pairs": len(drops),
        "auc_drop_min": min(drops) if drops else 0.0,
        "auc_drop_max": max(drops) if drops else 0.0,
        "auc_drop_mean": sum(drops) / len(drops) if drops else 0.0,
        "auc_drop_median": (
            sorted(drops)[len(drops) // 2] if drops else 0.0
        ),
    }

    # ── 4. Subsample-size sensitivity ──────────────────────────────
    pool_pairs = np.concatenate(
        [domains[d][_LABEL_POSITIVE] for d in domain_names], axis=0
    )
    full_c_hat = fit_c_hat(pool_pairs)
    sub_master_seed = int(seeds.get("subsample_master") or (config.seed + 3))
    subsample_results: Dict[str, Any] = {
        "pool_size": int(pool_pairs.shape[0]),
        "full_pool_c_hat_norm": float(np.linalg.norm(full_c_hat)),
        "by_size": {},
    }
    for size in config.subsample_sizes:
        size_int = int(size)
        if size_int < 1:
            continue
        if size_int > pool_pairs.shape[0]:
            subsample_results["by_size"][str(size_int)] = {
                "n_subsamples": 0,
                "skipped_reason": (
                    f"requested N={size_int} > pool size "
                    f"{pool_pairs.shape[0]}"
                ),
            }
            continue
        cosines: List[float] = []
        sub_rng = random.Random(sub_master_seed ^ (size_int * 0x9E3779B1))
        for _k in range(int(config.n_subsamples)):
            idx = sub_rng.sample(range(pool_pairs.shape[0]), size_int)
            try:
                c_sub = fit_c_hat(pool_pairs[idx])
            except ValueError:
                continue
            cosines.append(abs_cosine(c_sub, full_c_hat))
        ci_low, ci_high, mean, std = bootstrap_ci(
            cosines, ci=config.ci_percent
        )
        subsample_results["by_size"][str(size_int)] = {
            "n_subsamples": len(cosines),
            "abs_cosine_to_full_mean": mean,
            "abs_cosine_to_full_std": std,
            "abs_cosine_to_full_ci_low": ci_low,
            "abs_cosine_to_full_ci_high": ci_high,
            "ci_percent": config.ci_percent,
        }

    # ── 5. Decision ────────────────────────────────────────────────
    pairwise_min = pairwise_summary["min"] if pairwise_offdiag else 1.0
    auc_drop_med = cross_summary["auc_drop_median"] if drops else 0.0
    single_c_hat_holds = (
        pairwise_min >= _FALSIFICATION_PAIRWISE_COSINE
        and auc_drop_med <= _FALSIFICATION_AUC_DROP
    )
    decision = {
        "criterion": (
            f"single ĉ ⇔ pairwise abs-cosine min ≥ {_FALSIFICATION_PAIRWISE_COSINE} "
            f"AND median cross-domain AUC drop ≤ {_FALSIFICATION_AUC_DROP}"
        ),
        "pairwise_cosine_threshold": _FALSIFICATION_PAIRWISE_COSINE,
        "auc_drop_threshold": _FALSIFICATION_AUC_DROP,
        "observed_pairwise_cosine_min": pairwise_min,
        "observed_pairwise_cosine_summary": pairwise_summary,
        "observed_auc_drop_median": auc_drop_med,
        "observed_auc_drop_summary": cross_summary,
        "single_c_hat_holds": single_c_hat_holds,
        "outcome": (
            "single_c_hat_generalises"
            if single_c_hat_holds
            else "per_domain_c_hat_required"
        ),
    }

    # ── 6. Inputs / config / preregistration ───────────────────────
    inputs_digest = _digest_corpus(domains)
    inputs = {
        "corpus_source": corpus_source,
        "corpus_digest_sha256": inputs_digest,
        "model_id": corpus.get("model_id"),
        "model_version": corpus.get("model_version"),
        "dim": int(corpus["dim"]),
        "n_pairs_per_domain_label": {
            d: {
                _LABEL_POSITIVE: int(domains[d][_LABEL_POSITIVE].shape[0]),
                _LABEL_NEGATIVE: int(domains[d][_LABEL_NEGATIVE].shape[0]),
            }
            for d in domain_names
        },
        "n_domains": len(domain_names),
    }

    config_for_report = {
        "seed": int(config.seed),
        "n_bootstrap_iterations": int(config.n_bootstrap_iterations),
        "ci_percent": float(config.ci_percent),
        "n_subsamples": int(config.n_subsamples),
        "subsample_sizes": [int(s) for s in config.subsample_sizes],
        "minimum_pairs_per_domain_label": minimum_pairs,
        "corpus_source": corpus_source,
    }

    run_id = hashlib.sha256(
        f"{inputs_digest}|seed={config.seed}|"
        f"boot={config.n_bootstrap_iterations}|"
        f"sub={config.n_subsamples}|"
        f"sizes={list(config.subsample_sizes)}".encode("ascii")
    ).hexdigest()[:16]

    report = StabilityReport(
        schema_version=STABILITY_SCHEMA_VERSION,
        run_id=run_id,
        config=config_for_report,
        preregistration={
            "version": prereg.get("version"),
            "study_name": prereg.get("study_name"),
            "primary_hypothesis_id": (
                (prereg.get("primary_hypothesis") or {}).get("id")
            ),
            "domain_values": (prereg.get("dataset") or {}).get("domain_values"),
            "subsample_sizes": prereg.get("subsample_sizes"),
            "n_subsamples": prereg.get("n_subsamples"),
        },
        inputs=inputs,
        per_domain_c_hat=_round_floats(per_domain_summary),
        pairwise_cosine=_round_floats({
            "matrix": pairwise,
            "summary": pairwise_summary,
        }),
        cross_domain_auc=_round_floats({
            "within_domain_baseline": within,
            "cross_domain": cross,
            "summary": cross_summary,
        }),
        subsample_sensitivity=_round_floats(subsample_results),
        decision=_round_floats(decision),
        generated_with=_detect_optional_packages(),
    )
    return report


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c-hat-stability",
        description=(
            "Cross-domain stability study for the contradiction direction "
            "ĉ (prompt 48, Wave 13)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on the bundled tiny synthetic fixture and emit JSON to stdout.",
    )
    parser.add_argument(
        "--corpus",
        dest="corpus_path",
        type=str,
        default=None,
        help="Path to a per-domain pair-embeddings JSON.",
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
        "--n-subsamples",
        type=int,
        default=None,
        help="Override the subsample count for sensitivity analysis.",
    )
    parser.add_argument(
        "--preregistration",
        type=str,
        default=None,
        help="Override path to preregistration.yaml.",
    )
    parser.add_argument(
        "--minimum-pairs-per-domain-label",
        type=int,
        default=None,
        help="Override stopping_rule.minimum_pairs_per_domain_label (testing only).",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> StabilityConfig:
    prereg = load_preregistration(args.preregistration)
    boot = prereg.get("bootstrap") or {}
    seed = (
        args.seed if args.seed is not None
        else int(prereg.get("random_seed") or 4747)
    )
    n_boot = (
        args.n_bootstrap_iterations
        if args.n_bootstrap_iterations is not None
        else int(boot.get("iterations") or 1000)
    )
    n_sub = (
        args.n_subsamples
        if args.n_subsamples is not None
        else int(prereg.get("n_subsamples") or 50)
    )
    sizes_raw = prereg.get("subsample_sizes") or [200, 500, 1000]
    sizes = tuple(int(s) for s in sizes_raw)

    if args.dry_run:
        # Smaller iteration counts so dry-run finishes in a couple seconds
        # against the bundled fixture. Production runs use the full
        # pre-registered values.
        return StabilityConfig(
            seed=seed,
            n_bootstrap_iterations=args.n_bootstrap_iterations or 100,
            n_subsamples=args.n_subsamples or 10,
            subsample_sizes=(8, 16),
            ci_percent=95.0,
            fixture_path=str(DEFAULT_FIXTURE_PATH),
            preregistration_path=args.preregistration,
            minimum_pairs_override=args.minimum_pairs_per_domain_label or 4,
        )
    return StabilityConfig(
        seed=seed,
        n_bootstrap_iterations=n_boot,
        n_subsamples=n_sub,
        subsample_sizes=sizes,
        corpus_path=args.corpus_path,
        preregistration_path=args.preregistration,
        minimum_pairs_override=args.minimum_pairs_per_domain_label,
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
        report = run_stability_study(config)
    except InsufficientDomainSampleError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 3
    except StabilityError as exc:
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
