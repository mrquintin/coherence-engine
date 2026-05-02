"""Quarterly Model-Risk-Management report assembler (prompt 60, Wave 15).

This module consolidates the artifacts that the Coherence Engine
governance surfaces produce — validation studies, calibration drift
telemetry, override frequency, anti-gaming alert rates, decision
reproducibility audits, and a free-form known-weakness backlog — into
a single deterministic data structure (``MRMReportData``) that a
renderer can serialize to LaTeX (and via that, PDF).

The report is *informed by*, not legally compliant with, the OCC / Fed
SR 11-7 framework on Supervisory Guidance on Model Risk Management.
The framework gives a useful spine — purpose & limitations, ongoing
validation, monitoring, governance — and we mirror that spine here so
an outside reviewer who *does* operate under SR 11-7 can map our
artifacts onto theirs without having to invent the shape themselves.
We make no compliance claim; the disclaimer string ``MRM_DISCLAIMER``
is rendered verbatim into every report.

Determinism contract
--------------------

* Same ``MRMReportInputs`` (after canonical loading from disk) →
  byte-identical ``.tex`` source via the renderer. The renderer's
  determinism test is the cheapest signal of accidental drift in
  template ordering or in this assembler's aggregation logic, so the
  shapes here are sorted at every step that hands data to Jinja2.
* No wall-clock reads inside ``assemble_quarterly_report``. The
  ``generated_at`` field is supplied by the caller (the CLI does so);
  tests freeze it.
* Aggregates only — never include unredacted PII. Override actor IDs
  are hashed with a stable salt; override reason text is *not*
  included; partner identifiers appear only as opaque short hashes in
  per-partner tables.

The assembler is *defensive about missing inputs*: any source file
that is absent contributes an empty section rather than aborting the
report, because in early quarters several of these surfaces have not
yet accumulated data. The renderer marks empty sections so a reviewer
notices the absence rather than mistaking it for a clean bill of
health.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


MRM_REPORT_SCHEMA_VERSION = "mrm-report-v1"


MRM_DISCLAIMER = (
    "This report is informed by, not legally compliant with, the OCC / "
    "Fed SR 11-7 Supervisory Guidance on Model Risk Management. It is "
    "an internal practice document; no regulatory submission, legal "
    "compliance, or third-party-attestation claim is made or implied."
)


_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[3]

DEFAULT_BACKLOG_PATH = _REPO_ROOT / "data" / "governed" / "model_risk" / "backlog.yaml"
DEFAULT_TEMPLATE_PATH = (
    _REPO_ROOT / "data" / "governed" / "model_risk" / "templates" / "quarterly.tex.j2"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MRMReportError(RuntimeError):
    """Base class for MRM-report assembly failures."""


# ---------------------------------------------------------------------------
# QuarterRef
# ---------------------------------------------------------------------------


_QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")


@dataclass(frozen=True)
class QuarterRef:
    """A calendar quarter identifier, e.g. ``QuarterRef(2026, 2)`` for 2026Q2.

    The string form ``"2026Q2"`` is what the CLI accepts and what the
    renderer prints. ``QuarterRef.parse`` is strict: anything other
    than the ``YYYYQN`` form raises :class:`MRMReportError` so a
    typo'd quarter cannot quietly produce the wrong report.
    """

    year: int
    quarter: int

    def __post_init__(self) -> None:
        if not (1 <= int(self.quarter) <= 4):
            raise MRMReportError(f"quarter must be 1..4, got {self.quarter}")
        if int(self.year) < 1900 or int(self.year) > 9999:
            raise MRMReportError(f"year out of range: {self.year}")

    @classmethod
    def parse(cls, value: str) -> "QuarterRef":
        if not isinstance(value, str):
            raise MRMReportError(f"quarter ref must be a string: {value!r}")
        m = _QUARTER_RE.match(value.strip())
        if not m:
            raise MRMReportError(
                f"quarter ref must look like 'YYYYQN' (e.g. 2026Q2), got {value!r}"
            )
        return cls(year=int(m.group(1)), quarter=int(m.group(2)))

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.quarter}"

    def covers_iso_dates(self) -> Tuple[str, str]:
        """Return inclusive (start, end) ISO dates for the quarter."""

        starts = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
        ends = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
        return f"{self.year}-{starts[self.quarter]}", f"{self.year}-{ends[self.quarter]}"


# ---------------------------------------------------------------------------
# Input + output shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MRMReportInputs:
    """Where to read each section's source data from.

    Every field is optional. A missing file produces an empty section
    in the assembled report. The CLI populates these from
    ``data/governed/...`` defaults; tests inject inline dicts via
    :func:`assemble_quarterly_report_from_payload`.
    """

    quarter: QuarterRef
    generated_at: str  # ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' supplied by caller
    validation_study_path: Optional[Path] = None
    drift_telemetry_path: Optional[Path] = None
    override_stats_path: Optional[Path] = None
    anti_gaming_alert_stats_path: Optional[Path] = None
    reproducibility_audit_path: Optional[Path] = None
    backlog_path: Path = DEFAULT_BACKLOG_PATH
    model_purpose_path: Optional[Path] = None


@dataclass(frozen=True)
class ValidationStudySummary:
    """Compact slice of a validation-study report (prompt 44)."""

    schema_version: str
    n_known_outcome: int
    auc_roc: float
    brier_score: float
    primary_rejected_null: bool
    primary_alpha: float
    coherence_point_estimate: float
    coherence_ci_99_lower: float
    coherence_ci_99_upper: float
    data_hash: str


@dataclass(frozen=True)
class DriftIndicator:
    """One calibration-drift observation (per metric, per period)."""

    metric: str
    baseline_value: float
    current_value: float
    delta: float
    threshold: float
    breached: bool


@dataclass(frozen=True)
class OverridePartnerStat:
    """Override frequency per partner (partner ID is opaque-hashed)."""

    partner_hash: str
    n_overrides: int
    n_pass_to_reject: int
    n_reject_to_pass: int
    most_common_reason_code: str


@dataclass(frozen=True)
class AntiGamingAlertRate:
    period_label: str
    n_decisions: int
    n_alerts: int
    rate: float


@dataclass(frozen=True)
class ReproducibilityAudit:
    """Outcome of a decision-reproducibility spot check."""

    audit_id: str
    n_replays: int
    n_matching: int
    match_rate: float
    notes: str


@dataclass(frozen=True)
class BacklogItem:
    """One known-weakness or remediation item."""

    item_id: str
    title: str
    severity: str  # critical | high | medium | low
    status: str  # open | in_progress | mitigated | accepted
    owner: str
    target_quarter: str


@dataclass(frozen=True)
class MRMReportData:
    """Fully assembled report data, ready to feed Jinja2.

    The ``input_digest`` is a SHA-256 over the canonical JSON of the
    rest of this dataclass; the renderer prints it on every page so
    two PDFs claiming to be the same quarter can be compared at a
    glance.
    """

    schema_version: str
    quarter_label: str
    quarter_start: str
    quarter_end: str
    generated_at: str
    disclaimer: str
    model_purpose: str
    model_limitations: Tuple[str, ...]
    validation_summary: Optional[ValidationStudySummary]
    drift_indicators: Tuple[DriftIndicator, ...]
    override_partner_stats: Tuple[OverridePartnerStat, ...]
    override_total_count: int
    anti_gaming_rates: Tuple[AntiGamingAlertRate, ...]
    reproducibility_audits: Tuple[ReproducibilityAudit, ...]
    known_weaknesses: Tuple[BacklogItem, ...]
    remediation_backlog: Tuple[BacklogItem, ...]
    input_digest: str

    def to_canonical_dict(self) -> Dict[str, Any]:
        return _canonical_dict(self)

    def to_canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_canonical_dict(), sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")

    def report_digest(self) -> str:
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Tiny YAML reader (subset sufficient for backlog.yaml)
# ---------------------------------------------------------------------------


def _read_yaml_subset(text: str) -> Dict[str, Any]:
    """Parse the small YAML subset used by ``backlog.yaml``.

    Supports top-level scalar mapping plus a single list-of-mappings
    under each mapping key. We deliberately do not pull in PyYAML for
    a file this small; the parser raises :class:`MRMReportError` on
    anything it cannot interpret rather than guessing.
    """

    out: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: Optional[List[Dict[str, Any]]] = None
    current_item: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if line == "":
            continue
        if not line.startswith(" "):
            # top-level mapping key
            if ":" not in line:
                raise MRMReportError(f"unexpected top-level line: {raw!r}")
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                current_key = key
                current_list = []
                current_item = None
                out[current_key] = current_list
            else:
                out[key] = _coerce(val)
                current_key = None
                current_list = None
                current_item = None
            continue
        # indented line — must be inside a list under current_key
        if current_list is None:
            raise MRMReportError(
                f"indented line without an open list: {raw!r}"
            )
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            current_item = {}
            current_list.append(current_item)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if current_item is None:
            raise MRMReportError(f"item field without an open item: {raw!r}")
        if ":" not in stripped:
            raise MRMReportError(f"expected 'key: value' inside item: {raw!r}")
        k, _, v = stripped.partition(":")
        current_item[k.strip()] = _coerce(v.strip())
        # indent is acknowledged for readability checks; we don't enforce
        _ = indent
    return out


def _coerce(val: str) -> Any:
    if val == "":
        return ""
    if (val.startswith('"') and val.endswith('"')) or (
        val.startswith("'") and val.endswith("'")
    ):
        return val[1:-1]
    low = val.lower()
    if low in {"true", "yes"}:
        return True
    if low in {"false", "no"}:
        return False
    if low in {"null", "none", "~"}:
        return None
    if re.match(r"^-?\d+$", val):
        return int(val)
    if re.match(r"^-?\d+\.\d+$", val):
        return float(val)
    return val


# ---------------------------------------------------------------------------
# Loaders for each section
# ---------------------------------------------------------------------------


def _load_json_optional(path: Optional[Path]) -> Optional[Mapping[str, Any]]:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MRMReportError(f"could not parse JSON at {p}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MRMReportError(f"expected a JSON object at {p}")
    return payload


def _summarize_validation_study(
    payload: Optional[Mapping[str, Any]],
) -> Optional[ValidationStudySummary]:
    if payload is None:
        return None
    metrics = payload.get("metrics") or {}
    primary = payload.get("primary_hypothesis_result") or {}
    coefs = payload.get("coefficients") or []
    coh = next(
        (c for c in coefs if isinstance(c, Mapping) and c.get("name") == "coherence_score"),
        None,
    )
    return ValidationStudySummary(
        schema_version=str(payload.get("schema_version", "")),
        n_known_outcome=int(payload.get("n_known_outcome", 0) or 0),
        auc_roc=float(metrics.get("auc_roc", 0.0) or 0.0),
        brier_score=float(metrics.get("brier_score", 0.0) or 0.0),
        primary_rejected_null=bool(primary.get("rejected_null", False)),
        primary_alpha=float(primary.get("alpha", 0.0) or 0.0),
        coherence_point_estimate=float(coh.get("point", 0.0)) if coh else 0.0,
        coherence_ci_99_lower=float(coh.get("ci_lower_99", 0.0)) if coh else 0.0,
        coherence_ci_99_upper=float(coh.get("ci_upper_99", 0.0)) if coh else 0.0,
        data_hash=str(payload.get("data_hash", "")),
    )


def _drift_indicators_from(
    payload: Optional[Mapping[str, Any]],
) -> Tuple[DriftIndicator, ...]:
    if payload is None:
        return ()
    rows = payload.get("indicators") or []
    out: List[DriftIndicator] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        baseline = float(row.get("baseline_value", 0.0) or 0.0)
        current = float(row.get("current_value", 0.0) or 0.0)
        threshold = float(row.get("threshold", 0.0) or 0.0)
        delta = current - baseline
        breached = abs(delta) > threshold if threshold > 0 else False
        out.append(
            DriftIndicator(
                metric=str(row.get("metric", "")),
                baseline_value=round(baseline, 6),
                current_value=round(current, 6),
                delta=round(delta, 6),
                threshold=round(threshold, 6),
                breached=bool(breached),
            )
        )
    out.sort(key=lambda d: d.metric)
    return tuple(out)


_PARTNER_HASH_SALT = b"mrm-report-v1::partner-hash"


def _partner_hash(partner_id: str) -> str:
    h = hashlib.sha256(_PARTNER_HASH_SALT + str(partner_id).encode("utf-8")).hexdigest()
    return h[:12]


def _override_stats_from(
    payload: Optional[Mapping[str, Any]],
) -> Tuple[Tuple[OverridePartnerStat, ...], int]:
    if payload is None:
        return (), 0
    total = int(payload.get("total_overrides", 0) or 0)
    rows = payload.get("by_partner") or []
    out: List[OverridePartnerStat] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        partner_id = str(row.get("partner_id", ""))
        if not partner_id:
            continue
        out.append(
            OverridePartnerStat(
                partner_hash=_partner_hash(partner_id),
                n_overrides=int(row.get("n_overrides", 0) or 0),
                n_pass_to_reject=int(row.get("n_pass_to_reject", 0) or 0),
                n_reject_to_pass=int(row.get("n_reject_to_pass", 0) or 0),
                most_common_reason_code=str(row.get("most_common_reason_code", "")),
            )
        )
    out.sort(key=lambda r: r.partner_hash)
    if not total:
        total = sum(r.n_overrides for r in out)
    return tuple(out), int(total)


def _anti_gaming_from(
    payload: Optional[Mapping[str, Any]],
) -> Tuple[AntiGamingAlertRate, ...]:
    if payload is None:
        return ()
    rows = payload.get("series") or []
    out: List[AntiGamingAlertRate] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        n_dec = int(row.get("n_decisions", 0) or 0)
        n_alerts = int(row.get("n_alerts", 0) or 0)
        rate = (n_alerts / n_dec) if n_dec > 0 else 0.0
        out.append(
            AntiGamingAlertRate(
                period_label=str(row.get("period_label", "")),
                n_decisions=n_dec,
                n_alerts=n_alerts,
                rate=round(rate, 6),
            )
        )
    out.sort(key=lambda r: r.period_label)
    return tuple(out)


def _reproducibility_from(
    payload: Optional[Mapping[str, Any]],
) -> Tuple[ReproducibilityAudit, ...]:
    if payload is None:
        return ()
    rows = payload.get("audits") or []
    out: List[ReproducibilityAudit] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        n_replays = int(row.get("n_replays", 0) or 0)
        n_matching = int(row.get("n_matching", 0) or 0)
        match_rate = (n_matching / n_replays) if n_replays > 0 else 0.0
        out.append(
            ReproducibilityAudit(
                audit_id=str(row.get("audit_id", "")),
                n_replays=n_replays,
                n_matching=n_matching,
                match_rate=round(match_rate, 6),
                notes=str(row.get("notes", "")),
            )
        )
    out.sort(key=lambda r: r.audit_id)
    return tuple(out)


def _backlog_items(
    raw_list: Sequence[Mapping[str, Any]] | None,
) -> Tuple[BacklogItem, ...]:
    if not raw_list:
        return ()
    out: List[BacklogItem] = []
    for row in raw_list:
        if not isinstance(row, Mapping):
            continue
        out.append(
            BacklogItem(
                item_id=str(row.get("item_id", "")),
                title=str(row.get("title", "")),
                severity=str(row.get("severity", "medium")),
                status=str(row.get("status", "open")),
                owner=str(row.get("owner", "")),
                target_quarter=str(row.get("target_quarter", "")),
            )
        )
    out.sort(key=lambda r: (_severity_rank(r.severity), r.item_id))
    return tuple(out)


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _severity_rank(severity: str) -> int:
    return _SEVERITY_ORDER.get(severity.lower(), 99)


def _load_backlog(
    path: Path,
) -> Tuple[Tuple[BacklogItem, ...], Tuple[BacklogItem, ...], str, Tuple[str, ...]]:
    p = Path(path)
    if not p.is_file():
        return (), (), "", ()
    text = p.read_text(encoding="utf-8")
    try:
        doc = _read_yaml_subset(text)
    except MRMReportError:
        raise
    weaknesses = _backlog_items(doc.get("known_weaknesses"))
    remediation = _backlog_items(doc.get("remediation_backlog"))
    purpose = str(doc.get("model_purpose") or "")
    raw_lim = doc.get("model_limitations") or []
    limitations: List[str] = []
    if isinstance(raw_lim, list):
        for item in raw_lim:
            if isinstance(item, Mapping):
                desc = item.get("description")
                if desc:
                    limitations.append(str(desc))
            elif isinstance(item, str):
                limitations.append(item)
    return weaknesses, remediation, purpose, tuple(limitations)


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def _canonical_dict(data: MRMReportData) -> Dict[str, Any]:
    raw = {
        "anti_gaming_rates": [
            {
                "period_label": r.period_label,
                "n_decisions": r.n_decisions,
                "n_alerts": r.n_alerts,
                "rate": r.rate,
            }
            for r in data.anti_gaming_rates
        ],
        "disclaimer": data.disclaimer,
        "drift_indicators": [
            {
                "metric": d.metric,
                "baseline_value": d.baseline_value,
                "current_value": d.current_value,
                "delta": d.delta,
                "threshold": d.threshold,
                "breached": d.breached,
            }
            for d in data.drift_indicators
        ],
        "generated_at": data.generated_at,
        "input_digest": data.input_digest,
        "known_weaknesses": [_backlog_dict(b) for b in data.known_weaknesses],
        "model_limitations": list(data.model_limitations),
        "model_purpose": data.model_purpose,
        "override_partner_stats": [
            {
                "partner_hash": s.partner_hash,
                "n_overrides": s.n_overrides,
                "n_pass_to_reject": s.n_pass_to_reject,
                "n_reject_to_pass": s.n_reject_to_pass,
                "most_common_reason_code": s.most_common_reason_code,
            }
            for s in data.override_partner_stats
        ],
        "override_total_count": data.override_total_count,
        "quarter_end": data.quarter_end,
        "quarter_label": data.quarter_label,
        "quarter_start": data.quarter_start,
        "remediation_backlog": [_backlog_dict(b) for b in data.remediation_backlog],
        "reproducibility_audits": [
            {
                "audit_id": a.audit_id,
                "n_replays": a.n_replays,
                "n_matching": a.n_matching,
                "match_rate": a.match_rate,
                "notes": a.notes,
            }
            for a in data.reproducibility_audits
        ],
        "schema_version": data.schema_version,
        "validation_summary": (
            None
            if data.validation_summary is None
            else {
                "auc_roc": data.validation_summary.auc_roc,
                "brier_score": data.validation_summary.brier_score,
                "coherence_ci_99_lower": data.validation_summary.coherence_ci_99_lower,
                "coherence_ci_99_upper": data.validation_summary.coherence_ci_99_upper,
                "coherence_point_estimate": data.validation_summary.coherence_point_estimate,
                "data_hash": data.validation_summary.data_hash,
                "n_known_outcome": data.validation_summary.n_known_outcome,
                "primary_alpha": data.validation_summary.primary_alpha,
                "primary_rejected_null": data.validation_summary.primary_rejected_null,
                "schema_version": data.validation_summary.schema_version,
            }
        ),
    }
    return json.loads(json.dumps(raw, sort_keys=True, separators=(",", ":")))


def _backlog_dict(b: BacklogItem) -> Dict[str, Any]:
    return {
        "item_id": b.item_id,
        "owner": b.owner,
        "severity": b.severity,
        "status": b.status,
        "target_quarter": b.target_quarter,
        "title": b.title,
    }


def _compute_input_digest(
    *,
    quarter_label: str,
    generated_at: str,
    purpose: str,
    limitations: Sequence[str],
    validation_summary: Optional[ValidationStudySummary],
    drift: Sequence[DriftIndicator],
    overrides: Sequence[OverridePartnerStat],
    override_total: int,
    anti_gaming: Sequence[AntiGamingAlertRate],
    repros: Sequence[ReproducibilityAudit],
    weaknesses: Sequence[BacklogItem],
    remediation: Sequence[BacklogItem],
) -> str:
    """Hash everything that determines the report payload.

    The digest is deliberately *not* derived from a final
    ``MRMReportData`` instance because the digest itself is one of
    that instance's fields. Computing it from the inputs sidesteps
    the chicken-and-egg.
    """

    blob = {
        "anti_gaming": [
            (a.period_label, a.n_decisions, a.n_alerts, a.rate) for a in anti_gaming
        ],
        "drift": [
            (d.metric, d.baseline_value, d.current_value, d.delta, d.threshold, d.breached)
            for d in drift
        ],
        "generated_at": generated_at,
        "limitations": list(limitations),
        "overrides": [
            (
                s.partner_hash,
                s.n_overrides,
                s.n_pass_to_reject,
                s.n_reject_to_pass,
                s.most_common_reason_code,
            )
            for s in overrides
        ],
        "override_total": int(override_total),
        "purpose": purpose,
        "quarter": quarter_label,
        "remediation": [_backlog_dict(b) for b in remediation],
        "reproducibility": [
            (a.audit_id, a.n_replays, a.n_matching, a.match_rate, a.notes)
            for a in repros
        ],
        "validation_summary": (
            None
            if validation_summary is None
            else {
                "auc_roc": validation_summary.auc_roc,
                "brier_score": validation_summary.brier_score,
                "coh_ci_99_lo": validation_summary.coherence_ci_99_lower,
                "coh_ci_99_hi": validation_summary.coherence_ci_99_upper,
                "coh_point": validation_summary.coherence_point_estimate,
                "data_hash": validation_summary.data_hash,
                "n_known": validation_summary.n_known_outcome,
                "primary_alpha": validation_summary.primary_alpha,
                "primary_rejected": validation_summary.primary_rejected_null,
                "schema": validation_summary.schema_version,
            }
        ),
        "weaknesses": [_backlog_dict(b) for b in weaknesses],
    }
    s = json.dumps(blob, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assemble_quarterly_report(inputs: MRMReportInputs) -> MRMReportData:
    """Consolidate every governance source into one ``MRMReportData``.

    The function is the deterministic heart of the report pipeline:
    same inputs (after canonical loading) → identical output.

    Parameters
    ----------
    inputs:
        ``MRMReportInputs`` describing the quarter and the paths to
        each source artifact. Any path that points at a missing file
        is silently treated as an empty section so the report can be
        rendered even before every source has accumulated data.

    Returns
    -------
    :class:`MRMReportData` ready for the LaTeX renderer.
    """

    if not isinstance(inputs.quarter, QuarterRef):
        raise MRMReportError("inputs.quarter must be a QuarterRef")
    if not isinstance(inputs.generated_at, str) or not inputs.generated_at:
        raise MRMReportError("inputs.generated_at must be a non-empty ISO string")

    weaknesses, remediation, backlog_purpose, backlog_limitations = _load_backlog(
        inputs.backlog_path
    )

    purpose: str
    limitations: Tuple[str, ...]
    if inputs.model_purpose_path is not None:
        p = Path(inputs.model_purpose_path)
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            doc = _read_yaml_subset(text)
            purpose = str(doc.get("model_purpose") or backlog_purpose or "")
            raw_lim = doc.get("model_limitations") or list(backlog_limitations)
            lim_list: List[str] = []
            if isinstance(raw_lim, list):
                for item in raw_lim:
                    if isinstance(item, Mapping):
                        desc = item.get("description")
                        if desc:
                            lim_list.append(str(desc))
                    elif isinstance(item, str):
                        lim_list.append(item)
            limitations = tuple(lim_list)
        else:
            purpose = backlog_purpose
            limitations = backlog_limitations
    else:
        purpose = backlog_purpose
        limitations = backlog_limitations

    validation_payload = _load_json_optional(inputs.validation_study_path)
    validation_summary = _summarize_validation_study(validation_payload)

    drift_payload = _load_json_optional(inputs.drift_telemetry_path)
    drift = _drift_indicators_from(drift_payload)

    override_payload = _load_json_optional(inputs.override_stats_path)
    overrides, override_total = _override_stats_from(override_payload)

    anti_gaming_payload = _load_json_optional(inputs.anti_gaming_alert_stats_path)
    anti_gaming = _anti_gaming_from(anti_gaming_payload)

    repro_payload = _load_json_optional(inputs.reproducibility_audit_path)
    repros = _reproducibility_from(repro_payload)

    quarter_start, quarter_end = inputs.quarter.covers_iso_dates()

    digest = _compute_input_digest(
        quarter_label=inputs.quarter.label,
        generated_at=inputs.generated_at,
        purpose=purpose,
        limitations=limitations,
        validation_summary=validation_summary,
        drift=drift,
        overrides=overrides,
        override_total=override_total,
        anti_gaming=anti_gaming,
        repros=repros,
        weaknesses=weaknesses,
        remediation=remediation,
    )

    return MRMReportData(
        schema_version=MRM_REPORT_SCHEMA_VERSION,
        quarter_label=inputs.quarter.label,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        generated_at=inputs.generated_at,
        disclaimer=MRM_DISCLAIMER,
        model_purpose=purpose,
        model_limitations=limitations,
        validation_summary=validation_summary,
        drift_indicators=drift,
        override_partner_stats=overrides,
        override_total_count=int(override_total),
        anti_gaming_rates=anti_gaming,
        reproducibility_audits=repros,
        known_weaknesses=weaknesses,
        remediation_backlog=remediation,
        input_digest=digest,
    )


__all__ = [
    "AntiGamingAlertRate",
    "BacklogItem",
    "DEFAULT_BACKLOG_PATH",
    "DEFAULT_TEMPLATE_PATH",
    "DriftIndicator",
    "MRMReportData",
    "MRMReportError",
    "MRMReportInputs",
    "MRM_DISCLAIMER",
    "MRM_REPORT_SCHEMA_VERSION",
    "OverridePartnerStat",
    "QuarterRef",
    "ReproducibilityAudit",
    "ValidationStudySummary",
    "assemble_quarterly_report",
]
