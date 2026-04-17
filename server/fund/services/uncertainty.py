"""Deterministic calibrated uncertainty for coherence superiority (model-error proxy).

Uses observable scoring features to set a 95% nominal interval width around the
point estimate. Constants are fixed for revision ``fund-cs-superiority-v1``; they
are chosen so typical cases match the scale of the prior sqrt(n) heuristic while
widening under weak transcript signal, contradiction burden, and layer disagreement.

Runtime profiles: merge ``COHERENCE_UNCERTAINTY_PROFILE_PATH`` (JSON file) then
``COHERENCE_UNCERTAINTY_PROFILE`` (JSON object string) over defaults when unset
fields are omitted.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

UNCERTAINTY_MODEL_VERSION = "fund-cs-superiority-v1"

# Calibration constants (v1 defaults).
_SIGMA0 = 0.045
_Z95 = 1.96
_ALPHA_QUALITY = 0.55
_ALPHA_BURDEN = 0.35
_ALPHA_DISAGREEMENT = 0.90
_HALF_MIN = 0.025  # total CI width floor 0.05
_HALF_MAX = 0.125  # total CI width cap 0.25


@dataclass(frozen=True)
class UncertaintyParams:
    """Tunable uncertainty constants (deterministic; used at runtime and for calibration)."""

    sigma0: float = _SIGMA0
    z95: float = _Z95
    alpha_quality: float = _ALPHA_QUALITY
    alpha_burden: float = _ALPHA_BURDEN
    alpha_disagreement: float = _ALPHA_DISAGREEMENT
    half_min: float = _HALF_MIN
    half_max: float = _HALF_MAX


_DEFAULT_PARAMS = UncertaintyParams()


def _merge_profile_dict(base: UncertaintyParams, data: Mapping[str, object]) -> UncertaintyParams:
    def f(name: str, default: float) -> float:
        if name not in data or data[name] is None:
            return default
        return float(data[name])  # type: ignore[arg-type]

    return UncertaintyParams(
        sigma0=f("sigma0", base.sigma0),
        z95=f("z95", base.z95),
        alpha_quality=f("alpha_quality", base.alpha_quality),
        alpha_burden=f("alpha_burden", base.alpha_burden),
        alpha_disagreement=f("alpha_disagreement", base.alpha_disagreement),
        half_min=f("half_min", base.half_min),
        half_max=f("half_max", base.half_max),
    )


def resolve_uncertainty_params_from_environment() -> UncertaintyParams:
    """Load profile from env/file; defaults when nothing configured."""
    merged: Dict[str, object] = {}
    path = os.environ.get("COHERENCE_UNCERTAINTY_PROFILE_PATH", "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                file_obj = json.load(fh)
            if isinstance(file_obj, dict):
                merged.update(file_obj)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            # Graceful degradation: ignore malformed profile file and use defaults/env.
            pass
    raw = os.environ.get("COHERENCE_UNCERTAINTY_PROFILE", "").strip()
    if raw:
        try:
            env_obj = json.loads(raw)
            if isinstance(env_obj, dict):
                merged.update(env_obj)
        except (json.JSONDecodeError, ValueError, TypeError):
            # Graceful degradation: ignore malformed inline profile and use defaults.
            pass
    if not merged:
        return _DEFAULT_PARAMS
    try:
        return _merge_profile_dict(_DEFAULT_PARAMS, merged)
    except (ValueError, TypeError):
        return _DEFAULT_PARAMS


def layer_score_disagreement(layer_scores: Mapping[str, float]) -> float:
    """Population standard deviation of layer scores (spread as proxy for head disagreement)."""
    vals = [float(x) for x in layer_scores.values()]
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    var = sum((x - m) ** 2 for x in vals) / len(vals)
    return math.sqrt(var)


def contradiction_burden(n_contradictions: int, n_propositions: int) -> float:
    """Contradiction count normalized by expected proposition budget (same spirit as anti_gaming)."""
    n = max(1, int(n_propositions))
    return float(n_contradictions) / max(1.0, n / 3.0)


def calibrated_superiority_interval_95(
    superiority: float,
    n_propositions: int,
    transcript_quality: float,
    n_contradictions: int,
    layer_scores: Mapping[str, float],
    *,
    params: Optional[UncertaintyParams] = None,
) -> Tuple[float, float, Dict[str, object]]:
    """Return (lower, upper, calibration_metadata) for coherence superiority on [-1, 1]."""
    p = params if params is not None else resolve_uncertainty_params_from_environment()
    n = max(2, int(n_propositions))
    q = max(0.2, min(1.0, float(transcript_quality)))
    burden = contradiction_burden(n_contradictions, n)
    disagree = layer_score_disagreement(layer_scores)

    sigma = (
        p.sigma0
        / math.sqrt(n)
        * (1.0 + p.alpha_quality * (1.0 - q))
        * (1.0 + p.alpha_burden * min(1.5, burden))
        * (1.0 + p.alpha_disagreement * min(1.0, disagree * 4.0))
    )
    half = p.z95 * sigma
    half = max(p.half_min, min(p.half_max, half))

    lower = max(-1.0, float(superiority) - half)
    upper = min(1.0, float(superiority) + half)

    meta: Dict[str, object] = {
        "uncertainty_model_version": UNCERTAINTY_MODEL_VERSION,
        "calibration_inputs": {
            "n_propositions": n,
            "transcript_quality": round(q, 6),
            "contradiction_burden": round(burden, 6),
            "layer_disagreement_std": round(disagree, 6),
            "effective_sigma": round(sigma, 6),
            "half_width_95": round(half, 6),
        },
    }
    return lower, upper, meta
