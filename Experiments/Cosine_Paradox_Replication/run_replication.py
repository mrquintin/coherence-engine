"""Cosine Paradox replication harness (prompt 47, Wave 13).

This module is the deterministic, replayable core of the independent
replication of the Cosine Paradox headline claim — that, in raw
sentence-embedding cosine space, entailment and contradiction NLI pairs
are statistically indistinguishable.

Pipeline:

  1. Load a labeled NLI corpus (premise / hypothesis / label triples).
  2. Embed each premise and hypothesis with the production sentence
     encoder (``sentence-transformers/all-mpnet-base-v2``) and compute
     the cosine similarity per pair.
  3. Stratify cosines by label (entailment / contradiction / neutral).
  4. Report descriptive stats (mean, median, std, n) per label.
  5. Run a Mann-Whitney U test on (entailment, contradiction) and
     report the rank-biserial effect size with a 95 percent bootstrap
     CI plus a permutation p-value (n_permutations from the
     pre-registration).
  6. Emit a deterministic JSON report whose canonical bytes are stable
     across runs given the same seed and the same input cosines.

Determinism guarantees
----------------------

* Same ``ReplicationConfig`` (seed + same cosines) -> byte-identical
  ``ReplicationReport.to_canonical_bytes()`` output.
* Bootstrap + permutation use ``random.Random(config.seed)`` from the
  standard library — *not* numpy — so the harness runs with no
  third-party dependency. ``scipy`` / ``numpy`` are *only* consulted
  opportunistically and recorded in ``generated_with`` if available;
  the math is identical either way.
* No wall-clock reads inside the report; no live network reads unless
  ``--allow-network`` is passed.
* ``run_replication`` raises :class:`InsufficientSampleError` when any
  label group has fewer than the pre-registered minimum, *unless* the
  cosines source is a fixture (``"fixture"``), in which case the
  stopping rule is intentionally relaxed.

The on-disk preregistration is parsed with a deliberately tiny stdlib
YAML reader (``_parse_yaml``) — the schema is fixed and small enough
that pulling in PyYAML for it would be a needless dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPLICATION_SCHEMA_VERSION = "cosine-paradox-replication-v1"

_HERE = Path(__file__).resolve().parent
DEFAULT_PREREGISTRATION_PATH = _HERE / "preregistration.yaml"
DEFAULT_FIXTURE_PATH = _HERE / "fixtures" / "tiny_nli_fixture.json"
DEFAULT_EXPECTED_REPORT_PATH = _HERE / "expected_report.json"

_LABELS = ("entailment", "contradiction", "neutral")
_FALSIFICATION_EFFECT_THRESHOLD = 0.20  # |rank-biserial| >= 0.20 -> refute
# When the falsification thresholds are amended, bump preregistration.version.


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReplicationError(RuntimeError):
    """Base class for replication-harness failures."""


class InsufficientSampleError(ReplicationError):
    """Raised when any label group is below the pre-registered minimum."""

    def __init__(self, label: str, n: int, minimum: int):
        self.label = label
        self.n = int(n)
        self.minimum = int(minimum)
        super().__init__(
            f"INSUFFICIENT_SAMPLE: label={label!r} n={n} < minimum={minimum}; "
            f"refusing to emit report"
        )


class PreregistrationError(ReplicationError):
    """Raised when the on-disk preregistration is malformed."""


class NetworkAccessDenied(ReplicationError):
    """Raised when a network-required step is requested without --allow-network."""


# ---------------------------------------------------------------------------
# Tiny stdlib YAML reader — handles the subset used in preregistration.yaml.
# ---------------------------------------------------------------------------


def _read_yaml_lines(text: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    raw_lines = text.splitlines()
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # Folded scalar marker: collect continuation lines into one folded value.
        if stripped.endswith(": >-") or stripped.endswith(": >") or \
           stripped.endswith(": |") or stripped.endswith(": |-"):
            head, _, marker = stripped.rpartition(": ")
            i += 1
            collected: List[str] = []
            while i < len(raw_lines):
                next_raw = raw_lines[i]
                next_stripped = next_raw.lstrip(" ")
                next_indent = len(next_raw) - len(next_stripped)
                if not next_stripped:
                    if marker.startswith("|"):
                        collected.append("")
                    i += 1
                    continue
                if next_indent <= indent:
                    break
                collected.append(next_stripped)
                i += 1
            if marker.startswith(">"):
                joined = " ".join(collected).strip()
            else:
                joined = "\n".join(collected).strip()
            value_line = f'{head}: "{joined}"'
            out.append((indent, value_line))
            continue
        out.append((indent, stripped))
        i += 1
    return out


def _coerce_yaml_scalar(raw: str) -> Any:
    s = raw.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "None", "~", ""):
        return None
    try:
        if "." in s or "e" in s or "E" in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _parse_yaml(text: str) -> Dict[str, Any]:
    """Tiny YAML subset parser — mappings, lists, folded scalars."""

    lines = _read_yaml_lines(text)
    pos = 0

    def parse_block(min_indent: int) -> Tuple[Any, int]:
        nonlocal pos
        if pos >= len(lines):
            return {}, pos
        first_indent, first = lines[pos]
        if first_indent < min_indent:
            return {}, pos
        if first.startswith("- "):
            return parse_list(min_indent)
        return parse_mapping(min_indent)

    def parse_mapping(indent_target: int) -> Tuple[Dict[str, Any], int]:
        nonlocal pos
        out: Dict[str, Any] = {}
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < indent_target:
                break
            if indent > indent_target:
                raise PreregistrationError(
                    f"unexpected indentation {indent} (expected {indent_target}) "
                    f"at: {content!r}"
                )
            if content.startswith("- "):
                raise PreregistrationError(
                    f"unexpected list marker at indent {indent}: {content!r}"
                )
            if ":" not in content:
                raise PreregistrationError(
                    f"expected 'key: value' at indent {indent}: {content!r}"
                )
            key_part, _, val_part = content.partition(":")
            key = key_part.strip()
            val = val_part.strip()
            pos += 1
            if val == "":
                if pos < len(lines) and lines[pos][0] > indent_target:
                    nested_indent = lines[pos][0]
                    nested, pos2 = parse_block(nested_indent)
                    pos = pos2
                    out[key] = nested
                else:
                    out[key] = None
            else:
                out[key] = _coerce_yaml_scalar(val)
        return out, pos

    def parse_list(indent_target: int) -> Tuple[List[Any], int]:
        nonlocal pos
        out: List[Any] = []
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < indent_target:
                break
            if not content.startswith("- "):
                break
            inner = content[2:].strip()
            pos += 1
            if inner == "":
                if pos < len(lines) and lines[pos][0] > indent_target:
                    nested_indent = lines[pos][0]
                    nested, pos2 = parse_block(nested_indent)
                    pos = pos2
                    out.append(nested)
                else:
                    out.append(None)
            elif ":" in inner:
                key_part, _, val_part = inner.partition(":")
                key = key_part.strip()
                val = val_part.strip()
                item: Dict[str, Any] = {key: _coerce_yaml_scalar(val) if val else None}
                if pos < len(lines) and lines[pos][0] > indent_target:
                    nested_indent = lines[pos][0]
                    nested, pos2 = parse_mapping(nested_indent)
                    pos = pos2
                    for k, v in nested.items():
                        item[k] = v
                out.append(item)
            else:
                out.append(_coerce_yaml_scalar(inner))
        return out, pos

    result, _ = parse_mapping(0)
    return result


def load_preregistration(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    p = Path(path) if path is not None else DEFAULT_PREREGISTRATION_PATH
    text = p.read_text(encoding="utf-8")
    parsed = _parse_yaml(text)
    required = (
        "version", "study_name", "dataset", "embedding_model",
        "primary_hypothesis", "bootstrap", "random_seed", "stopping_rule",
    )
    missing = [k for k in required if k not in parsed]
    if missing:
        raise PreregistrationError(
            f"preregistration is missing required keys: {missing}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Statistics — stdlib only, two-sided tests, no scipy dependency.
# ---------------------------------------------------------------------------


def _ranks_with_ties(values: Sequence[float]) -> List[float]:
    """Return average ranks (1-based) of ``values``; ties get the average rank."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def mann_whitney_u(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float]:
    """Compute Mann-Whitney U (a vs b) and U' (b vs a). Returns (U_a, U_b)."""
    n1 = len(a)
    n2 = len(b)
    if n1 == 0 or n2 == 0:
        raise ValueError("mann_whitney_u: both groups must be non-empty")
    combined = list(a) + list(b)
    ranks = _ranks_with_ties(combined)
    R_a = sum(ranks[:n1])
    U_a = R_a - n1 * (n1 + 1) / 2.0
    U_b = n1 * n2 - U_a
    return U_a, U_b


def rank_biserial_effect_size(U_a: float, n1: int, n2: int) -> float:
    """Wendt's rank-biserial: r = 2*U_a / (n1*n2) - 1. Range [-1, 1]."""
    if n1 == 0 or n2 == 0:
        raise ValueError("rank_biserial_effect_size: empty group")
    return (2.0 * U_a) / (n1 * n2) - 1.0


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    if not sorted_values:
        raise ValueError("_percentile: empty list")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = p * (len(sorted_values) - 1) / 100.0
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_values[lo])
    frac = rank - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def bootstrap_rank_biserial_ci(
    a: Sequence[float],
    b: Sequence[float],
    *,
    iterations: int,
    seed: int,
    ci: float = 95.0,
) -> Tuple[float, float, float]:
    """Bootstrap CI for the rank-biserial effect size. Returns (low, high, std)."""
    rng = random.Random(seed)
    a_list = list(a)
    b_list = list(b)
    n1 = len(a_list)
    n2 = len(b_list)
    samples: List[float] = []
    for _ in range(iterations):
        ra = [a_list[rng.randrange(n1)] for _ in range(n1)]
        rb = [b_list[rng.randrange(n2)] for _ in range(n2)]
        U_a, _ = mann_whitney_u(ra, rb)
        samples.append(rank_biserial_effect_size(U_a, n1, n2))
    samples.sort()
    alpha = (100.0 - ci) / 2.0
    low = _percentile(samples, alpha)
    high = _percentile(samples, 100.0 - alpha)
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / max(1, len(samples) - 1)
    std = math.sqrt(var)
    return low, high, std


def permutation_test_u(
    a: Sequence[float],
    b: Sequence[float],
    *,
    iterations: int,
    seed: int,
) -> float:
    """Two-sided permutation p-value for the U statistic. Returns p in [0, 1]."""
    n1 = len(a)
    n2 = len(b)
    if n1 == 0 or n2 == 0:
        raise ValueError("permutation_test_u: empty group")
    combined = list(a) + list(b)
    U_obs, _ = mann_whitney_u(a, b)
    expected = n1 * n2 / 2.0
    obs_dev = abs(U_obs - expected)
    rng = random.Random(seed ^ 0x9E3779B97F4A7C15 & 0xFFFFFFFF)
    count = 0
    pool = list(combined)
    for _ in range(iterations):
        rng.shuffle(pool)
        sa = pool[:n1]
        sb = pool[n1:]
        U_perm, _ = mann_whitney_u(sa, sb)
        if abs(U_perm - expected) >= obs_dev:
            count += 1
    return (count + 1) / (iterations + 1)


def descriptive_stats(values: Sequence[float]) -> Dict[str, float]:
    """Mean, median, std (sample), min, max for a list of cosines."""
    if not values:
        return {"n": 0, "mean": 0.0, "median": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
    n = len(values)
    sorted_v = sorted(values)
    mean = sum(values) / n
    if n % 2 == 1:
        median = float(sorted_v[n // 2])
    else:
        median = (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2.0
    var = sum((x - mean) ** 2 for x in values) / max(1, n - 1)
    return {
        "n": n,
        "mean": mean,
        "median": median,
        "std": math.sqrt(var),
        "min": float(sorted_v[0]),
        "max": float(sorted_v[-1]),
    }


# ---------------------------------------------------------------------------
# Configuration + report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicationConfig:
    seed: int = 47
    n_permutations: int = 10_000
    n_bootstrap_iterations: int = 10_000
    alpha: float = 0.01
    ci_percent: float = 95.0
    cosines_path: Optional[str] = None  # JSON list of {label, cosine}
    fixture_path: Optional[str] = None  # alias for cosines_path with source="fixture"
    dataset_path: Optional[str] = None  # raw NLI .jsonl (premise/hypothesis/label)
    preregistration_path: Optional[str] = None
    embedder_id: str = "sentence-transformers/all-mpnet-base-v2"
    allow_network: bool = False
    minimum_n_per_label_override: Optional[int] = None


@dataclass
class ReplicationReport:
    schema_version: str
    run_id: str
    config: Dict[str, Any]
    preregistration: Dict[str, Any]
    inputs: Dict[str, Any]
    descriptive: Dict[str, Dict[str, float]]
    primary_test: Dict[str, Any]
    secondary_tests: Dict[str, Dict[str, Any]]
    falsification: Dict[str, Any]
    generated_with: Dict[str, Any]

    def to_canonical_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "config": self.config,
            "preregistration": self.preregistration,
            "inputs": self.inputs,
            "descriptive": self.descriptive,
            "primary_test": self.primary_test,
            "secondary_tests": self.secondary_tests,
            "falsification": self.falsification,
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
# Cosine loading
# ---------------------------------------------------------------------------


def load_cosines_json(path: os.PathLike) -> Dict[str, Any]:
    """Load a JSON file mapping each labeled pair to its cosine similarity."""
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ReplicationError(
            f"{p}: expected non-empty 'rows' list of {{label, cosine}} entries"
        )
    grouped: Dict[str, List[float]] = {label: [] for label in _LABELS}
    for row in rows:
        label = row.get("label")
        cosine = row.get("cosine")
        if label not in grouped:
            raise ReplicationError(
                f"{p}: unsupported label {label!r}; expected one of {_LABELS}"
            )
        if not isinstance(cosine, (int, float)):
            raise ReplicationError(
                f"{p}: cosine must be numeric, got {cosine!r}"
            )
        grouped[label].append(float(cosine))
    return {
        "source": payload.get("source", "unknown"),
        "model_id": payload.get("model_id"),
        "model_version": payload.get("model_version"),
        "schema": payload.get("schema"),
        "groups": grouped,
    }


def compute_cosines_from_dataset(
    dataset_rows: Sequence[Mapping[str, Any]],
    embedder: Any,
) -> Dict[str, List[float]]:
    """Embed premise/hypothesis pairs and compute cosine similarities.

    ``embedder`` must expose ``embed_batch(texts) -> list[list[float]]``.
    """
    if not dataset_rows:
        raise ReplicationError("compute_cosines_from_dataset: empty dataset")
    grouped: Dict[str, List[float]] = {label: [] for label in _LABELS}
    premises = [row["premise"] for row in dataset_rows]
    hypotheses = [row["hypothesis"] for row in dataset_rows]
    p_embs = embedder.embed_batch(premises)
    h_embs = embedder.embed_batch(hypotheses)
    for row, vp, vh in zip(dataset_rows, p_embs, h_embs):
        label = row.get("label") or row.get("gold_label")
        if label not in grouped:
            continue  # SNLI has '-' for adjudicated-undetermined; skip.
        grouped[label].append(_cosine(vp, vh))
    return grouped


def _cosine(u: Sequence[float], v: Sequence[float]) -> float:
    if len(u) != len(v):
        raise ValueError("_cosine: dimension mismatch")
    dot = 0.0
    nu = 0.0
    nv = 0.0
    for a, b in zip(u, v):
        dot += a * b
        nu += a * a
        nv += b * b
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return dot / (math.sqrt(nu) * math.sqrt(nv))


def _digest_groups(groups: Mapping[str, Sequence[float]]) -> str:
    h = hashlib.sha256()
    for label in _LABELS:
        h.update(label.encode("ascii"))
        h.update(b":")
        for v in groups.get(label, []):
            h.update(f"{v:.10f}".encode("ascii"))
            h.update(b",")
        h.update(b"|")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def _resolve_minimum_n(
    prereg: Mapping[str, Any], override: Optional[int]
) -> int:
    if override is not None:
        return int(override)
    rule = prereg.get("stopping_rule") or {}
    return int(rule.get("minimum_n_per_label", 200))


def _round_floats(obj: Any, digits: int = 10) -> Any:
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return obj
        return round(obj, digits)
    if isinstance(obj, list):
        return [_round_floats(x, digits) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_floats(v, digits) for k, v in obj.items()}
    return obj


def run_replication(config: ReplicationConfig) -> ReplicationReport:
    """Run the replication and return a deterministic report.

    The harness expects either ``cosines_path`` / ``fixture_path`` to
    point at a precomputed cosines JSON, *or* ``dataset_path`` to point
    at a raw NLI corpus together with a usable production embedder.
    The latter path requires either a local cached dataset+model or
    ``--allow-network`` to reach Hugging Face / Stanford.
    """

    prereg = load_preregistration(config.preregistration_path)
    seed = int(config.seed)

    cosines_payload: Dict[str, Any]
    cosines_source = "unknown"
    if config.cosines_path:
        cosines_payload = load_cosines_json(config.cosines_path)
        cosines_source = cosines_payload.get("source") or "cosines_json"
    elif config.fixture_path:
        cosines_payload = load_cosines_json(config.fixture_path)
        cosines_source = "fixture"
    elif config.dataset_path:
        if not config.allow_network:
            # The dataset path may be a local cached file; if missing we
            # would need to download. We cannot tell from here whether
            # the embedder will hit the network. The safer default is
            # to refuse and require an explicit opt-in.
            raise NetworkAccessDenied(
                "dataset embedding requires --allow-network (model + corpus "
                "downloads); pass --cosines-path to run on precomputed cosines"
            )
        from coherence_engine.embeddings.base import get_embedder

        embedder = get_embedder()
        dataset_rows = _load_nli_jsonl(config.dataset_path)
        groups = compute_cosines_from_dataset(dataset_rows, embedder)
        cosines_payload = {
            "source": "dataset",
            "model_id": getattr(embedder, "model_name", config.embedder_id),
            "model_version": None,
            "schema": "computed",
            "groups": groups,
        }
        cosines_source = "dataset"
    else:
        raise ReplicationError(
            "ReplicationConfig: must provide one of cosines_path, "
            "fixture_path, or dataset_path"
        )

    groups: Dict[str, List[float]] = cosines_payload["groups"]

    # Stopping rule (skipped for fixtures so the dry-run + tests can run).
    minimum_n = _resolve_minimum_n(prereg, config.minimum_n_per_label_override)
    if cosines_source != "fixture":
        for label in _LABELS:
            if len(groups.get(label, [])) < minimum_n:
                raise InsufficientSampleError(
                    label, len(groups.get(label, [])), minimum_n
                )

    descriptive = {label: descriptive_stats(groups[label]) for label in _LABELS}

    ent = groups["entailment"]
    con = groups["contradiction"]
    neu = groups["neutral"]

    primary = _run_pair_test(
        ent, con,
        iterations_perm=config.n_permutations,
        iterations_boot=config.n_bootstrap_iterations,
        seed=seed,
        alpha=config.alpha,
        ci_percent=config.ci_percent,
        falsification_threshold=_FALSIFICATION_EFFECT_THRESHOLD,
    )

    secondary: Dict[str, Dict[str, Any]] = {}
    if neu:
        secondary["neutral_vs_entailment"] = _run_pair_test(
            neu, ent,
            iterations_perm=config.n_permutations,
            iterations_boot=config.n_bootstrap_iterations,
            seed=seed + 1,
            alpha=0.05,
            ci_percent=config.ci_percent,
            falsification_threshold=None,
        )
        secondary["neutral_vs_contradiction"] = _run_pair_test(
            neu, con,
            iterations_perm=config.n_permutations,
            iterations_boot=config.n_bootstrap_iterations,
            seed=seed + 2,
            alpha=0.05,
            ci_percent=config.ci_percent,
            falsification_threshold=None,
        )

    inputs_digest = _digest_groups(groups)

    falsification = {
        "criterion": (
            f"|rank_biserial_effect_size| >= {_FALSIFICATION_EFFECT_THRESHOLD} "
            f"AND permutation_p_value < {config.alpha}"
        ),
        "effect_threshold": _FALSIFICATION_EFFECT_THRESHOLD,
        "alpha": config.alpha,
        "observed_effect": primary["rank_biserial_effect_size"],
        "observed_p_value": primary["permutation_p_value"],
        "paradox_refuted": (
            abs(primary["rank_biserial_effect_size"]) >= _FALSIFICATION_EFFECT_THRESHOLD
            and primary["permutation_p_value"] < config.alpha
        ),
    }
    falsification["outcome"] = (
        "paradox_refuted" if falsification["paradox_refuted"] else "paradox_confirmed"
    )

    config_for_report = {
        "seed": seed,
        "n_permutations": config.n_permutations,
        "n_bootstrap_iterations": config.n_bootstrap_iterations,
        "alpha": config.alpha,
        "ci_percent": config.ci_percent,
        "embedder_id": config.embedder_id,
        "cosines_source": cosines_source,
        "minimum_n_per_label": minimum_n,
    }

    inputs = {
        "cosines_source": cosines_source,
        "cosines_digest_sha256": inputs_digest,
        "model_id": cosines_payload.get("model_id"),
        "model_version": cosines_payload.get("model_version"),
        "n_per_label": {label: len(groups[label]) for label in _LABELS},
        "n_total": sum(len(groups[label]) for label in _LABELS),
    }

    run_id = hashlib.sha256(
        f"{inputs_digest}|seed={seed}|perm={config.n_permutations}|"
        f"boot={config.n_bootstrap_iterations}|alpha={config.alpha}".encode("ascii")
    ).hexdigest()[:16]

    generated_with = _detect_optional_packages()

    report = ReplicationReport(
        schema_version=REPLICATION_SCHEMA_VERSION,
        run_id=run_id,
        config=config_for_report,
        preregistration={
            "version": prereg.get("version"),
            "study_name": prereg.get("study_name"),
            "primary_hypothesis_id": (prereg.get("primary_hypothesis") or {}).get("id"),
            "alpha": (prereg.get("primary_hypothesis") or {}).get("alpha"),
            "n_permutations": (
                (prereg.get("primary_hypothesis") or {}).get("n_permutations")
            ),
            "leakage_assumption": prereg.get("leakage_assumption"),
        },
        inputs=inputs,
        descriptive=_round_floats(descriptive),
        primary_test=_round_floats(primary),
        secondary_tests=_round_floats(secondary),
        falsification=_round_floats(falsification),
        generated_with=generated_with,
    )
    return report


def _run_pair_test(
    a: Sequence[float],
    b: Sequence[float],
    *,
    iterations_perm: int,
    iterations_boot: int,
    seed: int,
    alpha: float,
    ci_percent: float,
    falsification_threshold: Optional[float],
) -> Dict[str, Any]:
    n1 = len(a)
    n2 = len(b)
    U_a, U_b = mann_whitney_u(a, b)
    U = min(U_a, U_b)
    effect = rank_biserial_effect_size(U_a, n1, n2)
    p_perm = permutation_test_u(
        a, b, iterations=iterations_perm, seed=seed
    )
    ci_low, ci_high, ci_std = bootstrap_rank_biserial_ci(
        a, b,
        iterations=iterations_boot,
        seed=seed + 7919,
        ci=ci_percent,
    )
    return {
        "n_a": n1,
        "n_b": n2,
        "mann_whitney_u": U,
        "u_a": U_a,
        "u_b": U_b,
        "rank_biserial_effect_size": effect,
        "rank_biserial_ci": [ci_low, ci_high],
        "rank_biserial_ci_percent": ci_percent,
        "rank_biserial_ci_std": ci_std,
        "permutation_p_value": p_perm,
        "n_permutations": iterations_perm,
        "n_bootstrap_iterations": iterations_boot,
        "alpha": alpha,
        "reject_null": p_perm < alpha,
        "falsification_threshold": falsification_threshold,
    }


def _load_nli_jsonl(path: os.PathLike) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _detect_optional_packages() -> Dict[str, Any]:
    info: Dict[str, Any] = {"python": "stdlib"}
    try:
        import numpy  # type: ignore

        info["numpy"] = numpy.__version__
    except Exception:
        info["numpy"] = None
    try:
        import scipy  # type: ignore

        info["scipy"] = scipy.__version__
    except Exception:
        info["scipy"] = None
    try:
        import sentence_transformers  # type: ignore

        info["sentence_transformers"] = sentence_transformers.__version__
    except Exception:
        info["sentence_transformers"] = None
    return info


# ---------------------------------------------------------------------------
# CLI entrypoint (also reachable through ``coherence_engine.cli``)
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cosine-paradox-replication",
        description=(
            "Independent replication of the Cosine Paradox claim "
            "(prompt 47, Wave 13)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on the bundled tiny fixture and emit the report to stdout.",
    )
    parser.add_argument(
        "--cosines",
        dest="cosines_path",
        type=str,
        default=None,
        help="Path to a precomputed cosines JSON ({rows: [{label, cosine}]}).",
    )
    parser.add_argument(
        "--dataset",
        dest="dataset_path",
        type=str,
        default=None,
        help="Path to a raw NLI .jsonl with premise/hypothesis/label.",
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
        "--n-permutations",
        type=int,
        default=None,
        help="Override the pre-registered permutation count (n_permutations).",
    )
    parser.add_argument(
        "--n-bootstrap-iterations",
        type=int,
        default=None,
        help="Override the bootstrap iteration count.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Permit network access for embedder + dataset download.",
    )
    parser.add_argument(
        "--preregistration",
        type=str,
        default=None,
        help="Override path to preregistration.yaml.",
    )
    parser.add_argument(
        "--minimum-n-per-label",
        type=int,
        default=None,
        help="Override stopping_rule.minimum_n_per_label (testing only).",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> ReplicationConfig:
    prereg = load_preregistration(args.preregistration)
    pri = prereg.get("primary_hypothesis") or {}
    boot = prereg.get("bootstrap") or {}
    seed = args.seed if args.seed is not None else int(prereg.get("random_seed") or 47)
    n_perm = (
        args.n_permutations
        if args.n_permutations is not None
        else int(pri.get("n_permutations") or 10_000)
    )
    n_boot = (
        args.n_bootstrap_iterations
        if args.n_bootstrap_iterations is not None
        else int(boot.get("iterations") or 10_000)
    )
    if args.dry_run:
        # The dry-run is purely a smoke test — run the harness end to end
        # against the bundled fixture using a *smaller* iteration count
        # (1000 / 1000) so the command finishes in seconds. The full
        # pre-registered counts (10000 / 10000) only kick in for real
        # replication runs against a labeled NLI corpus.
        return ReplicationConfig(
            seed=seed,
            n_permutations=args.n_permutations or 1000,
            n_bootstrap_iterations=args.n_bootstrap_iterations or 1000,
            alpha=float(pri.get("alpha") or 0.01),
            fixture_path=str(DEFAULT_FIXTURE_PATH),
            preregistration_path=args.preregistration,
            allow_network=False,
            minimum_n_per_label_override=args.minimum_n_per_label or 4,
        )
    return ReplicationConfig(
        seed=seed,
        n_permutations=n_perm,
        n_bootstrap_iterations=n_boot,
        alpha=float(pri.get("alpha") or 0.01),
        cosines_path=args.cosines_path,
        dataset_path=args.dataset_path,
        preregistration_path=args.preregistration,
        allow_network=args.allow_network,
        minimum_n_per_label_override=args.minimum_n_per_label,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    if not args.dry_run and not args.cosines_path and not args.dataset_path:
        print(
            "error: must pass one of --dry-run, --cosines, or --dataset",
            file=sys.stderr,
        )
        return 2
    config = _config_from_args(args)
    try:
        report = run_replication(config)
    except InsufficientSampleError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ReplicationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    canonical = report.to_canonical_bytes()
    if args.output:
        Path(args.output).write_bytes(canonical)
    else:
        sys.stdout.write(canonical.decode("ascii"))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
