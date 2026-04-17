"""Deterministic historical calibration for fund superiority uncertainty intervals."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from coherence_engine.server.fund.services.uncertainty import (
    UNCERTAINTY_MODEL_VERSION,
    UncertaintyParams,
    calibrated_superiority_interval_95,
)


def load_historical_records(path: str) -> List[MutableMapping[str, Any]]:
    """Load records from a JSON array file or JSONL (one object per line)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped[0] == "[":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON root must be an array of records")
        return [dict(r) for r in data]
    out: List[MutableMapping[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(dict(json.loads(line)))
    return out


def _coerce_float(obj: Any, keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        if k in obj and obj[k] is not None:
            return float(obj[k])
    return None


def _normalize_record(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return canonical fields or None if the row is unusable."""
    sup = _coerce_float(raw, ("coherence_superiority", "superiority"))
    y = _coerce_float(raw, ("outcome_superiority", "observed_superiority", "y"))
    if sup is None or y is None:
        return None
    n_props = raw.get("n_propositions")
    if n_props is None:
        return None
    n_props = int(n_props)
    tq = raw.get("transcript_quality")
    if tq is None:
        return None
    tq = float(tq)
    nc = raw.get("n_contradictions")
    if nc is None:
        nc = 0
    nc = int(nc)
    ls = raw.get("layer_scores")
    if not isinstance(ls, Mapping):
        return None
    layer_scores = {str(k): float(v) for k, v in ls.items()}
    return {
        "superiority": sup,
        "outcome": y,
        "n_propositions": n_props,
        "transcript_quality": tq,
        "n_contradictions": nc,
        "layer_scores": layer_scores,
    }


def normalize_records(raw_records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for r in raw_records:
        n = _normalize_record(r)
        if n is not None:
            normalized.append(n)
    return normalized


# Matches `data/governed/uncertainty_historical_outcomes.jsonl` layer_scores key order.
_GOVERNED_LAYER_SCORE_KEYS: Tuple[str, ...] = (
    "contradiction",
    "argumentation",
    "embedding",
    "compression",
    "structural",
)
# Public alias for export validators / ops docs (same tuple).
GOVERNED_LAYER_SCORE_KEY_ORDER: Tuple[str, ...] = _GOVERNED_LAYER_SCORE_KEYS


def _governed_layer_scores(ls: Mapping[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k in _GOVERNED_LAYER_SCORE_KEYS:
        if k in ls:
            out[k] = float(ls[k])
    for k in sorted(ls.keys()):
        sk = str(k)
        if sk not in out:
            out[sk] = float(ls[k])
    return out


def to_governed_jsonl_record(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a raw historical row to the governed JSONL shape (stable field order, no extras)."""
    n = _normalize_record(raw)
    if n is None:
        return None
    ls = _governed_layer_scores(n["layer_scores"])
    # Top-level key order matches committed `data/governed/uncertainty_historical_outcomes.jsonl` lines.
    return {
        "coherence_superiority": float(n["superiority"]),
        "outcome_superiority": float(n["outcome"]),
        "n_propositions": int(n["n_propositions"]),
        "transcript_quality": float(n["transcript_quality"]),
        "n_contradictions": int(n["n_contradictions"]),
        "layer_scores": ls,
    }


def _mean(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def evaluate_profile(
    records: Sequence[Mapping[str, Any]],
    params: UncertaintyParams,
    *,
    target_coverage: float = 0.95,
    width_penalty: float = 1.0,
) -> Tuple[float, Dict[str, float]]:
    """Return (loss, metrics) for a parameter profile."""
    if not records:
        return 0.0, {"coverage": 1.0, "mean_width": 0.0, "mean_absolute_error": 0.0}

    covered = 0
    widths: List[float] = []
    errors: List[float] = []

    for rec in records:
        lo, hi, _ = calibrated_superiority_interval_95(
            superiority=rec["superiority"],
            n_propositions=rec["n_propositions"],
            transcript_quality=rec["transcript_quality"],
            n_contradictions=rec["n_contradictions"],
            layer_scores=rec["layer_scores"],
            params=params,
        )
        y = float(rec["outcome"])
        if lo <= y <= hi:
            covered += 1
        widths.append(hi - lo)
        mid = 0.5 * (lo + hi)
        errors.append(abs(mid - y))

    coverage = covered / len(records)
    mean_w = _mean(widths)
    mae = _mean(errors)
    cov_err = coverage - float(target_coverage)
    loss = cov_err * cov_err + float(width_penalty) * mean_w * mean_w
    metrics = {
        "coverage": coverage,
        "mean_width": mean_w,
        "mean_absolute_error": mae,
        "n_evaluated": float(len(records)),
    }
    return loss, metrics


def _default_search_grid() -> Dict[str, Tuple[float, ...]]:
    """Fixed deterministic grids for exhaustive search."""
    return {
        "sigma0": (0.035, 0.045, 0.055),
        "alpha_quality": (0.45, 0.55, 0.65),
        "alpha_burden": (0.25, 0.35, 0.45),
        "alpha_disagreement": (0.70, 0.90, 1.10),
        "half_min": (0.020, 0.025),
        "half_max": (0.100, 0.125),
    }


def _param_tuple_key(p: UncertaintyParams) -> Tuple[float, ...]:
    return (
        p.sigma0,
        p.alpha_quality,
        p.alpha_burden,
        p.alpha_disagreement,
        p.half_min,
        p.half_max,
    )


def calibrate_from_records(
    records: Sequence[Mapping[str, Any]],
    *,
    target_coverage: float = 0.95,
    width_penalty: float = 1.0,
    grid: Optional[Mapping[str, Tuple[float, ...]]] = None,
) -> Dict[str, Any]:
    """
    Deterministic exhaustive grid search minimizing
    (coverage - target)^2 + width_penalty * mean_width^2.
    Ties break on lower mean_width, then lexicographic parameter tuple.
    """
    grid = dict(grid or _default_search_grid())
    base = UncertaintyParams()

    best_loss = math.inf
    best_metrics: Dict[str, float] = {}
    best_params: Optional[UncertaintyParams] = None

    for sigma0 in grid["sigma0"]:
        for aq in grid["alpha_quality"]:
            for ab in grid["alpha_burden"]:
                for ad in grid["alpha_disagreement"]:
                    for hm in grid["half_min"]:
                        for hx in grid["half_max"]:
                            if hm >= hx:
                                continue
                            p = UncertaintyParams(
                                sigma0=float(sigma0),
                                z95=base.z95,
                                alpha_quality=float(aq),
                                alpha_burden=float(ab),
                                alpha_disagreement=float(ad),
                                half_min=float(hm),
                                half_max=float(hx),
                            )
                            loss, metrics = evaluate_profile(
                                records,
                                p,
                                target_coverage=target_coverage,
                                width_penalty=width_penalty,
                            )
                            mw = metrics["mean_width"]
                            cand = (loss, mw, _param_tuple_key(p))
                            if best_params is None:
                                best_loss, best_metrics, best_params = loss, metrics, p
                                continue
                            prev = (
                                best_loss,
                                best_metrics["mean_width"],
                                _param_tuple_key(best_params),
                            )
                            if cand < prev:
                                best_loss, best_metrics, best_params = loss, metrics, p

    if best_params is None:
        best_params = base
        best_loss, best_metrics = evaluate_profile(
            records,
            best_params,
            target_coverage=target_coverage,
            width_penalty=width_penalty,
        )

    return {
        "uncertainty_model_version": UNCERTAINTY_MODEL_VERSION,
        "target_coverage": target_coverage,
        "width_penalty": width_penalty,
        "n_records": len(records),
        "best_parameters": asdict(best_params),
        "calibration_loss": best_loss,
        "metrics": best_metrics,
        "search": {
            "type": "exhaustive_grid",
            "grid_axes": {k: list(v) for k, v in sorted(grid.items())},
        },
    }


def run_calibration_pipeline(
    input_path: str,
    *,
    target_coverage: float = 0.95,
    width_penalty: float = 1.0,
) -> Dict[str, Any]:
    raw = load_historical_records(input_path)
    normalized = normalize_records(raw)
    result = calibrate_from_records(
        normalized,
        target_coverage=target_coverage,
        width_penalty=width_penalty,
    )
    result["input_path"] = str(Path(input_path).resolve())
    result["n_records_loaded"] = len(raw)
    result["n_records_used"] = len(normalized)
    result["n_records_skipped"] = len(raw) - len(normalized)
    return result
