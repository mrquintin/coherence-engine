"""Deterministic export of production scoring data as governed historical outcome rows.

Joins ``CoherenceScored`` event payloads (from DB outbox or raw JSON) with an
operator-provided outcomes annotation file to produce rows matching
``deploy/ops/uncertainty-historical-outcomes-export.example.json``.

No network calls.  All logic is pure-function except the optional DB query
helper at the bottom.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


@dataclass(frozen=True)
class CalibrationExportResult:
    rows: list[dict[str, Any]]
    skipped_no_outcome: int
    skipped_invalid: int
    warnings: tuple[str, ...]


def _derive_n_contradictions(
    anti_gaming: float, n_propositions: int
) -> int:
    """Best-effort inverse of the anti-gaming formula for legacy events.

    ``anti_gaming = min(1.0, (n_contradictions / max(1.0, n_props/3)) * 0.5)``
    When ``anti_gaming < 1.0`` the inverse is exact (integer round); when
    capped at 1.0 we return a lower-bound estimate.
    """
    denom = max(1.0, n_propositions / 3.0)
    raw = anti_gaming * denom * 2.0
    return max(0, round(raw))


def _extract_calibration_fields(
    event_payload: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Pull calibration-relevant fields from a single CoherenceScored payload."""
    cs = event_payload.get("coherence_superiority")
    if cs is None:
        return None
    layer_scores = event_payload.get("layer_scores")
    if not isinstance(layer_scores, Mapping):
        return None

    n_props = event_payload.get("n_propositions")
    tq = event_payload.get("transcript_quality_score")
    nc = event_payload.get("n_contradictions")

    if n_props is None or tq is None:
        return None

    if nc is None:
        ag = event_payload.get("anti_gaming_score")
        if ag is not None:
            nc = _derive_n_contradictions(float(ag), int(n_props))
        else:
            nc = 0

    return {
        "coherence_superiority": float(cs),
        "n_propositions": int(n_props),
        "transcript_quality": float(tq),
        "n_contradictions": int(nc),
        "layer_scores": {str(k): float(v) for k, v in layer_scores.items()},
    }


def load_outcomes_annotations(
    path: Path,
) -> Dict[str, float]:
    """Load ``{application_id: outcome_superiority, …}`` from JSON or JSONL.

    Accepted shapes:
    * JSON object ``{app_id: float, …}`` (flat mapping).
    * JSON array of ``{"application_id": "…", "outcome_superiority": float}``.
    * JSONL with one such object per line.
    """
    p = path.resolve()
    text = p.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return {}
    out: Dict[str, float] = {}
    if stripped[0] == "{":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            if "application_id" in obj and "outcome_superiority" in obj:
                out[str(obj["application_id"])] = float(obj["outcome_superiority"])
            else:
                for k, v in obj.items():
                    out[str(k)] = float(v)
            return out
    if stripped[0] == "[":
        rows = json.loads(text)
        for row in rows:
            if isinstance(row, dict) and "application_id" in row:
                out[str(row["application_id"])] = float(row["outcome_superiority"])
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict) and "application_id" in row:
            out[str(row["application_id"])] = float(row["outcome_superiority"])
    return out


def build_export_rows(
    scored_events: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, float],
    *,
    require_all_layer_keys: bool = False,
) -> CalibrationExportResult:
    """Join scored-event payloads with outcomes to produce governed export rows.

    Each element in *scored_events* should be a ``CoherenceScored`` event
    payload dict (or compatible) containing at least ``application_id``,
    ``coherence_superiority``, ``layer_scores``, ``n_propositions``,
    ``transcript_quality_score``.

    *outcomes* maps ``application_id`` → ``outcome_superiority`` (float).
    """
    from coherence_engine.server.fund.services.uncertainty_calibration import (
        GOVERNED_LAYER_SCORE_KEY_ORDER,
    )

    rows: List[Dict[str, Any]] = []
    skipped_no_outcome = 0
    skipped_invalid = 0
    warnings: List[str] = []

    for idx, evt in enumerate(scored_events):
        app_id = evt.get("application_id", "")
        if not app_id:
            skipped_invalid += 1
            warnings.append(f"event[{idx}]: missing application_id")
            continue

        outcome = outcomes.get(str(app_id))
        if outcome is None:
            skipped_no_outcome += 1
            continue

        fields = _extract_calibration_fields(evt)
        if fields is None:
            skipped_invalid += 1
            warnings.append(f"event[{idx}] app={app_id}: cannot extract calibration fields")
            continue

        if require_all_layer_keys:
            missing = [k for k in GOVERNED_LAYER_SCORE_KEY_ORDER if k not in fields["layer_scores"]]
            if missing:
                skipped_invalid += 1
                warnings.append(
                    f"event[{idx}] app={app_id}: layer_scores missing keys {missing!r}"
                )
                continue

        rows.append(
            {
                "coherence_superiority": fields["coherence_superiority"],
                "outcome_superiority": float(outcome),
                "n_propositions": fields["n_propositions"],
                "transcript_quality": fields["transcript_quality"],
                "n_contradictions": fields["n_contradictions"],
                "layer_scores": fields["layer_scores"],
            }
        )

    return CalibrationExportResult(
        rows=rows,
        skipped_no_outcome=skipped_no_outcome,
        skipped_invalid=skipped_invalid,
        warnings=tuple(warnings[:200]),
    )


def export_rows_to_json(rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize governed export rows as a pretty-printed JSON array."""
    return json.dumps(list(rows), indent=2, sort_keys=False) + "\n"


def export_rows_to_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize governed export rows as JSONL (one compact object per line)."""
    lines = [json.dumps(r, separators=(",", ":")) + "\n" for r in rows]
    return "".join(lines)


def extract_scored_events_from_outbox_rows(
    outbox_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Extract ``CoherenceScored`` payloads from raw outbox row dicts.

    Each outbox row is expected to have ``event_type`` and ``payload_json``
    (or already-parsed ``payload`` dict).
    """
    out: List[Dict[str, Any]] = []
    for row in outbox_rows:
        if row.get("event_type") != "CoherenceScored":
            continue
        payload = row.get("payload")
        if payload is None:
            raw = row.get("payload_json", "")
            if not raw:
                continue
            payload = json.loads(raw)
        if isinstance(payload, dict):
            out.append(payload)
    return out
