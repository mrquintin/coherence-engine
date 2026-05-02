"""Outcome-labeling service for the historical-startups validation corpus (prompt 43).

This module owns the *outcomes* layer that sits on top of the historical-pitch
manifest produced by :mod:`coherence_engine.server.fund.services.historical_corpus`.
For every pitch in the corpus we attach one or more realized-outcome rows
recording 5-year survival, exit event, last-known ARR / headcount, and a strict
provenance object (source + URL + retrieved_at + retrieved_by). Provenance is
**required** — unsourced labels are rejected at write time.

Public surface
--------------

* :func:`attach_outcome` — validate one outcome row against
  ``outcome_label.v1.json`` and append it to ``outcomes.jsonl``.
* :func:`audit` — load the manifest + outcomes file and report any pitch that
  does not yet have at least one outcome row with a non-null ``outcome_as_of``
  and a parseable URL.
* :func:`export` — join the corpus with the latest outcome (by
  ``outcome_as_of``) per ``pitch_id`` and return a study-ready dict with a
  deterministic row order. Rows whose latest label is ``unknown`` for either
  ``survival_5yr`` or ``exit_event`` are excluded by default.

The on-disk file ``data/historical_corpus/outcomes.jsonl`` accepts ``#``
prefixed comment lines for the file header; the loader skips them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from coherence_engine.server.fund.services.historical_corpus import (
    DEFAULT_CORPUS_ROOT,
    DEFAULT_MANIFEST_PATH,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"

_HERE = Path(__file__).resolve()
SCHEMA_PATH = _HERE.parent.parent / "schemas" / "datasets" / "outcome_label.v1.json"

DEFAULT_OUTCOMES_PATH = DEFAULT_CORPUS_ROOT / "outcomes.jsonl"

EXIT_EVENTS = ("acquired", "ipo", "shutdown", "active", "unknown")

PROVENANCE_SOURCES = (
    "crunchbase",
    "pitchbook",
    "sec_edgar",
    "company_blog",
    "news_archive",
    "operator_query",
)

_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutcomeLabelingError(Exception):
    """Base class for outcome-labeling failures."""


class OutcomeSchemaError(OutcomeLabelingError):
    """Raised when an outcome row fails ``outcome_label.v1.json`` validation."""

    def __init__(self, pitch_id: Optional[str], errors: List[str]):
        self.pitch_id = pitch_id
        self.errors = errors
        super().__init__(
            f"outcome row pitch_id={pitch_id!r} failed schema validation: "
            + "; ".join(errors)
        )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class AuditReport:
    """Returned by :func:`audit`."""

    manifest_path: str
    outcomes_path: str
    pitches_total: int = 0
    pitches_with_outcome: int = 0
    pitches_missing: List[str] = field(default_factory=list)
    rows_seen: int = 0
    rows_invalid: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.pitches_missing and not self.rows_invalid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest_path": self.manifest_path,
            "outcomes_path": self.outcomes_path,
            "pitches_total": self.pitches_total,
            "pitches_with_outcome": self.pitches_with_outcome,
            "pitches_missing": list(self.pitches_missing),
            "rows_seen": self.rows_seen,
            "rows_invalid": list(self.rows_invalid),
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _is_url_parseable(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return bool(parsed.scheme) and bool(parsed.netloc)


def _validate_outcome(row: Mapping[str, Any]) -> List[str]:
    """Mirror of ``outcome_label.v1.json``. Returns list of error strings."""

    errors: List[str] = []

    required_top = {
        "schema_version",
        "pitch_id",
        "survival_5yr",
        "exit_event",
        "last_known_arr_usd",
        "last_known_headcount",
        "outcome_as_of",
        "outcome_provenance",
    }
    missing = required_top - set(row.keys())
    if missing:
        errors.append(f"missing required keys: {sorted(missing)}")
    extra = set(row.keys()) - required_top
    if extra:
        errors.append(f"unexpected keys (additionalProperties=false): {sorted(extra)}")
    if missing:
        return errors

    if row.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION!r}, got {row.get('schema_version')!r}"
        )

    pid = row.get("pitch_id")
    if not isinstance(pid, str) or not _UUID_V7_RE.match(pid):
        errors.append("pitch_id must be a UUIDv7 lowercase-hex string")

    surv = row.get("survival_5yr")
    if not (isinstance(surv, bool) or surv == "unknown"):
        errors.append("survival_5yr must be true, false, or the string 'unknown'")

    if row.get("exit_event") not in EXIT_EVENTS:
        errors.append(f"exit_event must be one of {EXIT_EVENTS}")

    arr = row.get("last_known_arr_usd")
    if arr is not None:
        if isinstance(arr, bool) or not isinstance(arr, (int, float)) or arr < 0:
            errors.append("last_known_arr_usd must be null or a non-negative number")

    hc = row.get("last_known_headcount")
    if hc is not None:
        if isinstance(hc, bool) or not isinstance(hc, int) or hc < 0:
            errors.append("last_known_headcount must be null or a non-negative integer")

    asof = row.get("outcome_as_of")
    if not isinstance(asof, str) or not _DATE_RE.match(asof):
        errors.append("outcome_as_of must be an ISO-8601 date (YYYY-MM-DD)")

    pv = row.get("outcome_provenance")
    if not isinstance(pv, dict):
        errors.append("outcome_provenance must be an object")
    else:
        pv_required = {"source", "url", "retrieved_at", "retrieved_by"}
        pv_missing = pv_required - set(pv.keys())
        if pv_missing:
            errors.append(f"outcome_provenance missing keys: {sorted(pv_missing)}")
        pv_extra = set(pv.keys()) - pv_required
        if pv_extra:
            errors.append(f"outcome_provenance extra keys: {sorted(pv_extra)}")
        if pv.get("source") not in PROVENANCE_SOURCES:
            errors.append(
                f"outcome_provenance.source must be one of {PROVENANCE_SOURCES}"
            )
        if not _is_url_parseable(pv.get("url")):
            errors.append("outcome_provenance.url must be a parseable URL with scheme+netloc")
        for k in ("retrieved_at", "retrieved_by"):
            v = pv.get(k)
            if not isinstance(v, str) or not v:
                errors.append(f"outcome_provenance.{k} must be a non-empty string")

    return errors


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _iter_outcome_rows(outcomes_path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    """Yield (line_number, row_dict) for non-comment, non-empty lines."""

    if not outcomes_path.exists():
        return
    with outcomes_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                yield line_no, {"__decode_error__": True, "__raw__": stripped}
                continue
            if isinstance(obj, dict):
                yield line_no, obj


def _load_manifest_pitch_ids(manifest_path: Path) -> List[str]:
    ids: List[str] = []
    if not manifest_path.exists():
        return ids
    with manifest_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            pid = row.get("pitch_id") if isinstance(row, dict) else None
            if isinstance(pid, str):
                ids.append(pid)
    return ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attach_outcome(
    pitch_id: str,
    outcome: Mapping[str, Any],
    *,
    outcomes_path: Optional[os.PathLike[str] | str] = None,
) -> None:
    """Validate ``outcome`` and append it to ``outcomes.jsonl``.

    The ``pitch_id`` argument must match ``outcome['pitch_id']``; this surfaces
    typos before they hit disk. Validation is strict — any schema violation
    raises :class:`OutcomeSchemaError` and nothing is written.
    """

    target = Path(outcomes_path) if outcomes_path else DEFAULT_OUTCOMES_PATH
    row = dict(outcome)
    row.setdefault("schema_version", SCHEMA_VERSION)

    if row.get("pitch_id") != pitch_id:
        raise OutcomeSchemaError(
            pitch_id,
            [f"pitch_id argument {pitch_id!r} != row['pitch_id'] {row.get('pitch_id')!r}"],
        )

    errors = _validate_outcome(row)
    if errors:
        raise OutcomeSchemaError(pitch_id, errors)

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def audit(
    *,
    manifest_path: Optional[os.PathLike[str] | str] = None,
    outcomes_path: Optional[os.PathLike[str] | str] = None,
) -> AuditReport:
    """Check that every pitch in the manifest has at least one outcome row
    with a non-null ``outcome_as_of`` and a parseable ``outcome_provenance.url``.

    Invalid rows (decode errors, schema failures, URL not parseable, missing
    ``outcome_as_of``) are reported but do not count toward
    ``pitches_with_outcome``. Pitches missing entirely are reported in
    ``pitches_missing``.
    """

    target_manifest = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
    target_outcomes = Path(outcomes_path) if outcomes_path else DEFAULT_OUTCOMES_PATH

    manifest_ids = _load_manifest_pitch_ids(target_manifest)
    report = AuditReport(
        manifest_path=str(target_manifest),
        outcomes_path=str(target_outcomes),
        pitches_total=len(manifest_ids),
    )

    valid_pitch_ids: set = set()
    for line_no, row in _iter_outcome_rows(target_outcomes):
        report.rows_seen += 1
        if row.get("__decode_error__"):
            report.rows_invalid.append(
                {"line": line_no, "reason": "json_decode_error"}
            )
            continue
        errors = _validate_outcome(row)
        if errors:
            report.rows_invalid.append(
                {
                    "line": line_no,
                    "pitch_id": row.get("pitch_id"),
                    "errors": errors,
                }
            )
            continue
        # Schema-valid row: add the pitch_id to the valid set.
        valid_pitch_ids.add(row["pitch_id"])

    manifest_id_set = set(manifest_ids)
    report.pitches_with_outcome = sum(
        1 for pid in manifest_id_set if pid in valid_pitch_ids
    )
    report.pitches_missing = sorted(manifest_id_set - valid_pitch_ids)
    return report


def export(
    *,
    manifest_path: Optional[os.PathLike[str] | str] = None,
    outcomes_path: Optional[os.PathLike[str] | str] = None,
    include_unknown: bool = False,
) -> Dict[str, Any]:
    """Join the corpus with the latest outcome per pitch_id and return a
    deterministic, study-ready frame.

    Selection rule:
      * For each pitch_id, choose the schema-valid outcome row with the
        greatest ``outcome_as_of`` (lex-sortable ISO date). Ties broken by
        original file order (last-write wins).
      * Rows whose chosen outcome has ``survival_5yr == 'unknown'`` or
        ``exit_event == 'unknown'`` are dropped from the export by default —
        ``include_unknown=True`` keeps them.

    Output rows are sorted by ``pitch_id`` for determinism.
    """

    target_manifest = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
    target_outcomes = Path(outcomes_path) if outcomes_path else DEFAULT_OUTCOMES_PATH

    # Index manifest rows by pitch_id.
    manifest_by_id: Dict[str, Dict[str, Any]] = {}
    if target_manifest.exists():
        with target_manifest.open("r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                pid = row.get("pitch_id") if isinstance(row, dict) else None
                if isinstance(pid, str):
                    manifest_by_id[pid] = row

    # Pick latest valid outcome per pitch_id.
    latest_by_id: Dict[str, Tuple[str, int, Dict[str, Any]]] = {}
    for line_no, row in _iter_outcome_rows(target_outcomes):
        if row.get("__decode_error__"):
            continue
        if _validate_outcome(row):
            continue
        pid = row["pitch_id"]
        asof = row["outcome_as_of"]
        prev = latest_by_id.get(pid)
        if prev is None or (asof, line_no) >= (prev[0], prev[1]):
            latest_by_id[pid] = (asof, line_no, row)

    rows: List[Dict[str, Any]] = []
    excluded_unknown = 0
    for pid in sorted(latest_by_id.keys()):
        if pid not in manifest_by_id:
            # Outcome attached to a pitch_id we don't have in the manifest;
            # skip silently (audit will surface this separately if needed).
            continue
        outcome = latest_by_id[pid][2]
        is_unknown = (
            outcome.get("survival_5yr") == "unknown"
            or outcome.get("exit_event") == "unknown"
        )
        if is_unknown and not include_unknown:
            excluded_unknown += 1
            continue
        manifest_row = manifest_by_id[pid]
        rows.append(
            {
                "pitch_id": pid,
                "company_name": manifest_row.get("company_name"),
                "domain_primary": manifest_row.get("domain_primary"),
                "pitch_year": manifest_row.get("pitch_year"),
                "country": manifest_row.get("country"),
                "eligibility": manifest_row.get("eligibility"),
                "survival_5yr": outcome.get("survival_5yr"),
                "exit_event": outcome.get("exit_event"),
                "last_known_arr_usd": outcome.get("last_known_arr_usd"),
                "last_known_headcount": outcome.get("last_known_headcount"),
                "outcome_as_of": outcome.get("outcome_as_of"),
                "outcome_provenance": outcome.get("outcome_provenance"),
            }
        )

    return {
        "manifest_path": str(target_manifest),
        "outcomes_path": str(target_outcomes),
        "rows": rows,
        "row_count": len(rows),
        "excluded_unknown": excluded_unknown,
        "include_unknown": include_unknown,
    }


__all__ = [
    "AuditReport",
    "DEFAULT_OUTCOMES_PATH",
    "EXIT_EVENTS",
    "OutcomeLabelingError",
    "OutcomeSchemaError",
    "PROVENANCE_SOURCES",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "attach_outcome",
    "audit",
    "export",
]
