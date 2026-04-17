"""Deterministic merge of governed uncertainty historical outcome JSONL files (local-only).

Used to fold operator-exported batches into ``data/governed/uncertainty_historical_outcomes.jsonl``
without network calls. Regenerates a SHA-256 manifest compatible with
``uncertainty-profile verify-dataset``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from coherence_engine.server.fund.services.uncertainty_calibration import (
    GOVERNED_LAYER_SCORE_KEY_ORDER,
    load_historical_records,
    to_governed_jsonl_record,
)


def fingerprint_governed_record(rec: Mapping[str, Any]) -> str:
    """Stable digest for deduplication (sort_keys JSON of the governed record)."""
    g = to_governed_jsonl_record(rec)
    if g is None:
        raise ValueError("record is not a valid historical outcome row")
    body = json.dumps(g, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def format_governed_line(rec: Mapping[str, Any]) -> str:
    """Serialize one governed row to a JSONL line (legacy key order, compact separators)."""
    g = to_governed_jsonl_record(rec)
    if g is None:
        raise ValueError("record is not a valid historical outcome row")
    ordered = {
        "coherence_superiority": g["coherence_superiority"],
        "outcome_superiority": g["outcome_superiority"],
        "n_propositions": g["n_propositions"],
        "transcript_quality": g["transcript_quality"],
        "n_contradictions": g["n_contradictions"],
        "layer_scores": {k: float(v) for k, v in g["layer_scores"].items()},
    }
    return json.dumps(ordered, separators=(",", ":")) + "\n"


def _load_governed_rows(path: Path, *, strict: bool) -> tuple[list[dict[str, Any]], int]:
    raw = load_historical_records(str(path))
    out: list[dict[str, Any]] = []
    skipped = 0
    for row in raw:
        g = to_governed_jsonl_record(row)
        if g is None:
            skipped += 1
            if strict:
                raise ValueError(f"invalid historical record in {path}")
        else:
            out.append(g)
    return out, skipped


@dataclass(frozen=True)
class GovernedDatasetMergeResult:
    body: bytes
    manifest: dict[str, Any]
    provenance: dict[str, Any]


def merge_governed_historical_datasets(
    base_path: Path,
    incoming_paths: list[Path],
    *,
    dataset_name: str | None = None,
    prefer: Literal["incoming", "base"] = "incoming",
    strict_incoming: bool = False,
) -> GovernedDatasetMergeResult:
    """
    Merge JSON/JSONL historical rows into a governed JSONL byte blob + manifest.

    When ``incoming_paths`` is empty, returns the base file bytes unchanged (no reordering).
    """
    base_path = base_path.resolve()
    if not base_path.is_file():
        raise FileNotFoundError(f"base dataset not found: {base_path}")

    ds_name = dataset_name or base_path.name

    if not incoming_paths:
        body = base_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        manifest = {
            "dataset": ds_name,
            "algorithm": "sha256",
            "checksum_sha256": digest,
        }
        prov: dict[str, Any] = {
            "base_path": str(base_path),
            "incoming_paths": [],
            "prefer": prefer,
            "n_output_records": len([ln for ln in body.splitlines() if ln.strip()]),
            "pass_through": True,
        }
        return GovernedDatasetMergeResult(body=body, manifest=manifest, provenance=prov)

    base_rows, base_skip = _load_governed_rows(base_path, strict=True)
    if base_skip:
        raise ValueError(f"base dataset {base_path} contained {base_skip} invalid rows")

    merged: dict[str, dict[str, Any]] = {}
    for rec in base_rows:
        fp = fingerprint_governed_record(rec)
        merged[fp] = rec

    incoming_loaded = 0
    incoming_skipped = 0
    resolved_incoming: list[Path] = []
    for ip in incoming_paths:
        p = ip.resolve()
        resolved_incoming.append(p)
        if not p.is_file():
            raise FileNotFoundError(f"incoming dataset not found: {p}")
        rows, skipped = _load_governed_rows(p, strict=strict_incoming)
        incoming_skipped += skipped
        for rec in rows:
            incoming_loaded += 1
            fp = fingerprint_governed_record(rec)
            if fp in merged and prefer == "base":
                continue
            merged[fp] = rec

    lines = [format_governed_line(merged[k]) for k in sorted(merged.keys())]
    text = "".join(lines)
    body = text.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    manifest = {
        "dataset": ds_name,
        "algorithm": "sha256",
        "checksum_sha256": digest,
    }
    provenance = {
        "base_path": str(base_path),
        "incoming_paths": [str(x) for x in resolved_incoming],
        "prefer": prefer,
        "strict_incoming": strict_incoming,
        "n_base_records": len(base_rows),
        "n_incoming_records_accepted": incoming_loaded,
        "n_incoming_records_skipped_invalid": incoming_skipped,
        "n_output_records": len(merged),
        "pass_through": False,
    }
    return GovernedDatasetMergeResult(body=body, manifest=manifest, provenance=provenance)


@dataclass(frozen=True)
class HistoricalOutcomesExportValidation:
    """Result of ``validate_historical_outcomes_export`` (local file only)."""

    ok: bool
    source_path: str
    rows_total: int
    valid_rows: int
    invalid_rows: int
    require_standard_layer_keys: bool
    errors: tuple[str, ...]


def validate_historical_outcomes_export(
    path: Path,
    *,
    require_standard_layer_keys: bool = False,
) -> HistoricalOutcomesExportValidation:
    """
    Validate a JSON array or JSONL file intended for ``merge-historical-dataset`` / merge script.

    Rows must normalize via ``to_governed_jsonl_record`` (same rules as calibration).
    Optional ``require_standard_layer_keys`` enforces all keys in
    ``GOVERNED_LAYER_SCORE_KEY_ORDER`` on the governed ``layer_scores`` dict.
    """
    p = path.resolve()
    if not p.is_file():
        raise FileNotFoundError(f"export file not found: {p}")
    raw = load_historical_records(str(p))
    errors: list[str] = []
    valid = 0
    invalid = 0
    for idx, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            errors.append(f"record {idx}: root must be object, got {type(row).__name__}")
            invalid += 1
            continue
        g = to_governed_jsonl_record(row)
        if g is None:
            errors.append(
                f"record {idx}: cannot normalize (need coherence/superiority and outcome "
                "floats, n_propositions, transcript_quality, layer_scores object)"
            )
            invalid += 1
            continue
        if require_standard_layer_keys:
            ls = g.get("layer_scores") or {}
            missing = [k for k in GOVERNED_LAYER_SCORE_KEY_ORDER if k not in ls]
            if missing:
                errors.append(f"record {idx}: layer_scores missing keys {missing!r}")
                invalid += 1
                continue
        valid += 1
    rows_total = len(raw)
    ok = invalid == 0
    return HistoricalOutcomesExportValidation(
        ok=ok,
        source_path=str(p),
        rows_total=rows_total,
        valid_rows=valid,
        invalid_rows=invalid,
        require_standard_layer_keys=require_standard_layer_keys,
        errors=tuple(errors[:200]),
    )
