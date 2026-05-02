"""Historical-startups validation corpus — schema + ingestion harness (prompt 42).

This module owns the data layer for the predictive-validity study: 500 anonymized
historical founder pitches, each one pointing at object-storage URIs for the
source artifacts (transcript, deck, memo) and carrying eligibility flags that
gate it into the cohort. Outcome labels are attached separately (prompt 43).

Public surface
--------------

* :class:`EligibilityFlags` — pure dataclass mirror of the on-disk
  ``eligibility`` block.
* :func:`compute_eligibility` — pure function: input row → flags. Re-runnable
  from any context (no I/O), used both at ingest time and during ``validate``.
* :func:`ingest` — load rows from a directory of ``.json`` files (or a single
  file), validate them against ``historical_pitch.v1.json``, recompute
  eligibility, and append accepted rows to ``manifest.jsonl``. The default is
  ``dry_run=True``; nothing is written unless the caller flips it.
* :func:`validate` — re-validate every row in ``manifest.jsonl`` against the
  schema and recompute eligibility. Used by CI / the operator to catch drift.
* :func:`stat` — deterministic summary of the manifest (counts by source,
  domain, eligibility-pass rate). The numbers are pinned in the test suite.

Consent invariant
-----------------

Every real founder pitch in the corpus must have written consent recorded
in ``provenance.consent_documented = true``. Synthetic rows are exempt
(``provenance.source = "synthetic"``) but ingest still refuses any non-synthetic
row with ``consent_documented=false``. See ``docs/specs/historical_corpus.md``.

The harness deliberately does NOT mutate ``data/historical_corpus/`` outside
the ``ingest`` / ``validate`` paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"

# Path to the JSON Schema definition. Resolved relative to this file so that
# importers do not need to know the package layout.
_HERE = Path(__file__).resolve()
SCHEMA_PATH = (
    _HERE.parent.parent / "schemas" / "datasets" / "historical_pitch.v1.json"
)

# Canonical corpus root inside the repo. Tests can override via parameters.
DEFAULT_CORPUS_ROOT = _HERE.parents[3] / "data" / "historical_corpus"
DEFAULT_MANIFEST_PATH = DEFAULT_CORPUS_ROOT / "manifest.jsonl"
DEFAULT_SEEDS_DIR = DEFAULT_CORPUS_ROOT / "seeds"

# Eligibility thresholds. Pinned constants — bumping them is a corpus-version
# change (would require a schema bump from v1 → v2).
DATE_WINDOW_MIN_YEAR = 2005
DATE_WINDOW_MAX_YEAR = 2024
EVIDENCE_FLOOR_MIN_PROPOSITIONS = 10
EVIDENCE_FLOOR_MIN_METRICS = 3
EVIDENCE_FLOOR_MIN_SOURCES = 2

# Pitches that overlap with the training corpus must be excluded. The training
# corpus pitch ids live in this small in-tree allowlist; in production the file
# would be populated by the training-data extraction pipeline.
TRAINING_CORPUS_PITCH_IDS: frozenset = frozenset()

PROVENANCE_SOURCES = (
    "crunchbase",
    "cb_insights",
    "operator_archive",
    "public_filings",
    "synthetic",
)

DOMAIN_PRIMARY = (
    "fintech",
    "healthtech",
    "biotech",
    "deeptech",
    "consumer",
    "enterprise_saas",
    "marketplace",
    "climate",
    "edtech",
    "other",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HistoricalCorpusError(Exception):
    """Base class for historical-corpus failures."""


class SchemaValidationError(HistoricalCorpusError):
    """Raised when a row fails ``historical_pitch.v1.json`` validation."""

    def __init__(self, pitch_id: str, errors: List[str]):
        self.pitch_id = pitch_id
        self.errors = errors
        super().__init__(
            f"row pitch_id={pitch_id!r} failed schema validation: "
            + "; ".join(errors)
        )


class ConsentMissingError(HistoricalCorpusError):
    """Raised when a non-synthetic row is missing documented consent."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EligibilityFlags:
    """Pure mirror of the on-disk ``eligibility`` block.

    All four flags must be ``True`` for a row to be eligible for the cohort
    used by the predictive-validity study.
    """

    date_window_ok: bool
    evidence_floor_ok: bool
    no_training_overlap_ok: bool
    consent_documented: bool

    @property
    def all_ok(self) -> bool:
        return (
            self.date_window_ok
            and self.evidence_floor_ok
            and self.no_training_overlap_ok
            and self.consent_documented
        )

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass
class IngestionReport:
    """Returned by :func:`ingest`.

    The ``rows_written`` list is empty when ``dry_run=True``; the rest of the
    fields are computed regardless of dry-run mode so callers can preview the
    outcome of a real run.
    """

    source: str
    ingestion_run_id: str
    candidates_seen: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rejections: List[Dict[str, Any]] = field(default_factory=list)
    rows_written: List[str] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "ingestion_run_id": self.ingestion_run_id,
            "candidates_seen": self.candidates_seen,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "rejections": list(self.rejections),
            "rows_written": list(self.rows_written),
            "dry_run": self.dry_run,
        }


@dataclass
class ValidationReport:
    """Returned by :func:`validate` — schema + eligibility re-check of the manifest."""

    manifest_path: str
    rows_seen: int = 0
    rows_ok: int = 0
    rows_failed: int = 0
    failures: List[Dict[str, Any]] = field(default_factory=list)
    eligibility_drift: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest_path": self.manifest_path,
            "rows_seen": self.rows_seen,
            "rows_ok": self.rows_ok,
            "rows_failed": self.rows_failed,
            "failures": list(self.failures),
            "eligibility_drift": list(self.eligibility_drift),
        }


# ---------------------------------------------------------------------------
# Schema validation (no external deps — the ``jsonschema`` package is optional
# in this project, so we ship a small targeted validator that mirrors the
# v1 schema. Bumping the schema requires updating both files.)
# ---------------------------------------------------------------------------


_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ANON_NAME_RE = re.compile(r"^anon_[0-9a-f]{16}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_URI_RE = re.compile(r"^coh://[a-z]+/[a-z0-9_-]+/[A-Za-z0-9._/-]+$")


def _validate_row(row: Mapping[str, Any]) -> List[str]:
    """Return a list of validation error strings; empty list = valid.

    Mirrors ``historical_pitch.v1.json``. Kept in sync by hand because the
    project does not require ``jsonschema`` to be importable.
    """

    errors: List[str] = []

    required_top = {
        "schema_version",
        "pitch_id",
        "company_name",
        "domain_primary",
        "pitch_year",
        "country",
        "transcript_uri",
        "deck_uri",
        "memo_uri",
        "evidence_floor",
        "eligibility",
        "provenance",
    }
    missing = required_top - set(row.keys())
    if missing:
        errors.append(f"missing required keys: {sorted(missing)}")
    extra = set(row.keys()) - required_top
    if extra:
        errors.append(f"unexpected keys (additionalProperties=false): {sorted(extra)}")
    # Stop early if structure is fundamentally wrong — downstream checks would
    # only produce noise.
    if missing:
        return errors

    if row.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION!r}, got {row.get('schema_version')!r}"
        )
    if not isinstance(row.get("pitch_id"), str) or not _UUID_V7_RE.match(row["pitch_id"]):
        errors.append("pitch_id must be a UUIDv7 lowercase-hex string")
    if not isinstance(row.get("company_name"), str) or not _ANON_NAME_RE.match(
        row["company_name"]
    ):
        errors.append("company_name must match ^anon_[0-9a-f]{16}$")
    if row.get("domain_primary") not in DOMAIN_PRIMARY:
        errors.append(f"domain_primary must be one of {DOMAIN_PRIMARY}")

    pitch_year = row.get("pitch_year")
    if (
        not isinstance(pitch_year, int)
        or isinstance(pitch_year, bool)
        or pitch_year < 2005
        or pitch_year > 2030
    ):
        errors.append("pitch_year must be an integer in [2005, 2030]")

    if not isinstance(row.get("country"), str) or not _COUNTRY_RE.match(row["country"]):
        errors.append("country must be ISO 3166-1 alpha-2 (^[A-Z]{2}$)")

    for uri_key in ("transcript_uri", "deck_uri", "memo_uri"):
        v = row.get(uri_key)
        if not isinstance(v, str) or not _URI_RE.match(v):
            errors.append(f"{uri_key} must match coh://<backend>/<bucket>/<key>")

    ev = row.get("evidence_floor")
    if not isinstance(ev, dict):
        errors.append("evidence_floor must be an object")
    else:
        ev_required = {"n_propositions", "n_metrics_cited", "n_sources_cited"}
        ev_missing = ev_required - set(ev.keys())
        if ev_missing:
            errors.append(f"evidence_floor missing keys: {sorted(ev_missing)}")
        ev_extra = set(ev.keys()) - ev_required
        if ev_extra:
            errors.append(f"evidence_floor extra keys: {sorted(ev_extra)}")
        for k in ev_required & set(ev.keys()):
            v = ev[k]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                errors.append(f"evidence_floor.{k} must be a non-negative integer")

    el = row.get("eligibility")
    if not isinstance(el, dict):
        errors.append("eligibility must be an object")
    else:
        el_required = {
            "date_window_ok",
            "evidence_floor_ok",
            "no_training_overlap_ok",
            "consent_documented",
        }
        el_missing = el_required - set(el.keys())
        if el_missing:
            errors.append(f"eligibility missing keys: {sorted(el_missing)}")
        el_extra = set(el.keys()) - el_required
        if el_extra:
            errors.append(f"eligibility extra keys: {sorted(el_extra)}")
        for k in el_required & set(el.keys()):
            if not isinstance(el[k], bool):
                errors.append(f"eligibility.{k} must be a boolean")

    pv = row.get("provenance")
    if not isinstance(pv, dict):
        errors.append("provenance must be an object")
    else:
        pv_required = {
            "source",
            "ingested_at",
            "ingestion_run_id",
            "consent_documented",
        }
        pv_missing = pv_required - set(pv.keys())
        if pv_missing:
            errors.append(f"provenance missing keys: {sorted(pv_missing)}")
        pv_extra = set(pv.keys()) - pv_required
        if pv_extra:
            errors.append(f"provenance extra keys: {sorted(pv_extra)}")
        if pv.get("source") not in PROVENANCE_SOURCES:
            errors.append(f"provenance.source must be one of {PROVENANCE_SOURCES}")
        if not isinstance(pv.get("ingested_at"), str) or not pv["ingested_at"]:
            errors.append("provenance.ingested_at must be a non-empty string")
        if not isinstance(pv.get("ingestion_run_id"), str) or not pv["ingestion_run_id"]:
            errors.append("provenance.ingestion_run_id must be a non-empty string")
        if not isinstance(pv.get("consent_documented"), bool):
            errors.append("provenance.consent_documented must be a boolean")

    return errors


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compute_eligibility(row: Mapping[str, Any]) -> EligibilityFlags:
    """Compute eligibility flags from a row's *content*, ignoring any flags
    already on the row.

    Pure: no I/O. Used at ingest (to fill in or override the row's flags) and
    at validate (to detect drift between stored flags and recomputed ones).
    """

    pitch_year = row.get("pitch_year")
    date_window_ok = (
        isinstance(pitch_year, int)
        and not isinstance(pitch_year, bool)
        and DATE_WINDOW_MIN_YEAR <= pitch_year <= DATE_WINDOW_MAX_YEAR
    )

    ev = row.get("evidence_floor") or {}
    evidence_floor_ok = (
        isinstance(ev.get("n_propositions"), int)
        and isinstance(ev.get("n_metrics_cited"), int)
        and isinstance(ev.get("n_sources_cited"), int)
        and ev["n_propositions"] >= EVIDENCE_FLOOR_MIN_PROPOSITIONS
        and ev["n_metrics_cited"] >= EVIDENCE_FLOOR_MIN_METRICS
        and ev["n_sources_cited"] >= EVIDENCE_FLOOR_MIN_SOURCES
    )

    no_training_overlap_ok = row.get("pitch_id") not in TRAINING_CORPUS_PITCH_IDS

    pv = row.get("provenance") or {}
    if pv.get("source") == "synthetic":
        consent_documented = True
    else:
        consent_documented = bool(pv.get("consent_documented", False))

    return EligibilityFlags(
        date_window_ok=date_window_ok,
        evidence_floor_ok=evidence_floor_ok,
        no_training_overlap_ok=no_training_overlap_ok,
        consent_documented=consent_documented,
    )


def _new_ingestion_run_id(source: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    payload = f"{source}:{when.isoformat()}".encode("utf-8")
    return "ing_" + hashlib.sha256(payload).hexdigest()[:16]


def _iter_candidate_paths(source_path: Path) -> Iterable[Path]:
    if source_path.is_file():
        yield source_path
        return
    if source_path.is_dir():
        for p in sorted(source_path.glob("*.json")):
            yield p


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest(
    source_path: os.PathLike[str] | str,
    *,
    source: str,
    dry_run: bool = True,
    manifest_path: Optional[os.PathLike[str] | str] = None,
    now: Optional[datetime] = None,
) -> IngestionReport:
    """Ingest pitches from ``source_path`` and (optionally) append to manifest.

    ``source_path`` is either a file (a single row) or a directory of
    ``*.json`` files. ``source`` must be one of ``PROVENANCE_SOURCES``.

    Eligibility flags on the input row are *recomputed*; the harness writes the
    canonical flags so downstream consumers can rely on a single source of
    truth.

    Non-synthetic rows missing ``provenance.consent_documented = true`` are
    rejected with :class:`ConsentMissingError`.
    """

    if source not in PROVENANCE_SOURCES:
        raise ValueError(
            f"unknown provenance source {source!r}; must be one of {PROVENANCE_SOURCES}"
        )

    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"source path does not exist: {src}")

    target_manifest = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
    run_id = _new_ingestion_run_id(source, now)
    ingested_at = (now or datetime.now(timezone.utc)).isoformat()
    report = IngestionReport(source=source, ingestion_run_id=run_id, dry_run=dry_run)

    # Track ids already in the manifest so re-ingest is idempotent.
    existing_ids = _load_manifest_ids(target_manifest) if target_manifest.exists() else set()

    accepted_rows: List[Dict[str, Any]] = []

    for path in _iter_candidate_paths(src):
        report.candidates_seen += 1
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            report.rows_rejected += 1
            report.rejections.append(
                {"path": str(path), "reason": f"unreadable: {exc}"}
            )
            continue

        if not isinstance(raw, dict):
            report.rows_rejected += 1
            report.rejections.append(
                {"path": str(path), "reason": "row is not a JSON object"}
            )
            continue

        # Stamp provenance fields supplied by the harness; preserve any
        # caller-set source/consent flags so we can validate them.
        pv = dict(raw.get("provenance") or {})
        pv.setdefault("source", source)
        pv["ingested_at"] = ingested_at
        pv["ingestion_run_id"] = run_id
        pv.setdefault("consent_documented", False)
        raw["provenance"] = pv

        # Consent invariant: real pitches must have documented consent.
        if pv["source"] != "synthetic" and not pv.get("consent_documented"):
            report.rows_rejected += 1
            report.rejections.append(
                {
                    "path": str(path),
                    "pitch_id": raw.get("pitch_id"),
                    "reason": "consent_missing: non-synthetic row requires provenance.consent_documented=true",
                }
            )
            continue

        # Recompute and write back the canonical eligibility block.
        flags = compute_eligibility(raw)
        raw["eligibility"] = flags.to_dict()
        raw.setdefault("schema_version", SCHEMA_VERSION)

        errors = _validate_row(raw)
        if errors:
            report.rows_rejected += 1
            report.rejections.append(
                {
                    "path": str(path),
                    "pitch_id": raw.get("pitch_id"),
                    "reason": "schema_validation_failed",
                    "errors": errors,
                }
            )
            continue

        if raw["pitch_id"] in existing_ids:
            report.rows_rejected += 1
            report.rejections.append(
                {
                    "path": str(path),
                    "pitch_id": raw["pitch_id"],
                    "reason": "duplicate: pitch_id already in manifest",
                }
            )
            continue

        report.rows_accepted += 1
        accepted_rows.append(raw)
        existing_ids.add(raw["pitch_id"])

    if not dry_run and accepted_rows:
        target_manifest.parent.mkdir(parents=True, exist_ok=True)
        with target_manifest.open("a", encoding="utf-8") as fh:
            for row in accepted_rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
                report.rows_written.append(row["pitch_id"])

    return report


def validate(
    manifest_path: Optional[os.PathLike[str] | str] = None,
) -> ValidationReport:
    """Re-validate every row in ``manifest_path`` against the v1 schema and
    recompute eligibility flags. Returns a :class:`ValidationReport`.

    Drift between stored and recomputed eligibility is recorded in
    ``eligibility_drift`` (rows still count as ``rows_ok`` if the schema
    validates — drift is a soft signal that the row needs to be re-ingested).
    """

    target_manifest = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
    report = ValidationReport(manifest_path=str(target_manifest))

    if not target_manifest.exists():
        return report

    with target_manifest.open("r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            report.rows_seen += 1
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                report.rows_failed += 1
                report.failures.append(
                    {"line": line_no, "reason": f"json_decode_error: {exc}"}
                )
                continue

            errors = _validate_row(row) if isinstance(row, dict) else ["not an object"]
            if errors:
                report.rows_failed += 1
                report.failures.append(
                    {
                        "line": line_no,
                        "pitch_id": row.get("pitch_id") if isinstance(row, dict) else None,
                        "errors": errors,
                    }
                )
                continue

            report.rows_ok += 1

            stored = row.get("eligibility") or {}
            recomputed = compute_eligibility(row).to_dict()
            if stored != recomputed:
                report.eligibility_drift.append(
                    {
                        "line": line_no,
                        "pitch_id": row["pitch_id"],
                        "stored": stored,
                        "recomputed": recomputed,
                    }
                )

    return report


def stat(
    manifest_path: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    """Return a deterministic summary of the corpus manifest.

    Shape::

        {
          "manifest_path": "...",
          "total_rows": int,
          "by_source": {source: int, ...},
          "by_domain": {domain: int, ...},
          "by_year": {year_str: int, ...},
          "eligibility": {
            "all_ok": int,
            "date_window_ok": int,
            "evidence_floor_ok": int,
            "no_training_overlap_ok": int,
            "consent_documented": int
          }
        }

    Counts are taken from stored eligibility flags (no recomputation), so the
    output of ``stat`` reflects the manifest as written.
    """

    target_manifest = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
    summary: Dict[str, Any] = {
        "manifest_path": str(target_manifest),
        "total_rows": 0,
        "by_source": {s: 0 for s in PROVENANCE_SOURCES},
        "by_domain": {d: 0 for d in DOMAIN_PRIMARY},
        "by_year": {},
        "eligibility": {
            "all_ok": 0,
            "date_window_ok": 0,
            "evidence_floor_ok": 0,
            "no_training_overlap_ok": 0,
            "consent_documented": 0,
        },
    }

    if not target_manifest.exists():
        return summary

    with target_manifest.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue

            summary["total_rows"] += 1
            src = (row.get("provenance") or {}).get("source")
            if src in summary["by_source"]:
                summary["by_source"][src] += 1
            dom = row.get("domain_primary")
            if dom in summary["by_domain"]:
                summary["by_domain"][dom] += 1
            year = row.get("pitch_year")
            if isinstance(year, int):
                key = str(year)
                summary["by_year"][key] = summary["by_year"].get(key, 0) + 1

            el = row.get("eligibility") or {}
            for k in ("date_window_ok", "evidence_floor_ok", "no_training_overlap_ok", "consent_documented"):
                if el.get(k) is True:
                    summary["eligibility"][k] += 1
            if all(el.get(k) is True for k in (
                "date_window_ok", "evidence_floor_ok", "no_training_overlap_ok", "consent_documented"
            )):
                summary["eligibility"]["all_ok"] += 1

    summary["by_year"] = dict(sorted(summary["by_year"].items()))
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_manifest_ids(manifest_path: Path) -> set:
    ids: set = set()
    with manifest_path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            pid = row.get("pitch_id") if isinstance(row, dict) else None
            if isinstance(pid, str):
                ids.add(pid)
    return ids


__all__ = [
    "EligibilityFlags",
    "IngestionReport",
    "ValidationReport",
    "HistoricalCorpusError",
    "SchemaValidationError",
    "ConsentMissingError",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "DEFAULT_CORPUS_ROOT",
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_SEEDS_DIR",
    "PROVENANCE_SOURCES",
    "DOMAIN_PRIMARY",
    "compute_eligibility",
    "ingest",
    "validate",
    "stat",
]
