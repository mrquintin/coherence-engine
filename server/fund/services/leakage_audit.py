"""Leakage audit + temporal-holdout integrity check (prompt 45, Wave 12).

The leakage audit is the gate that runs *before* any validation study
report can be rendered. It enforces three invariants on the joined
study config:

1. **No training/holdout overlap.** For every artifact in
   ``data/governed/training_artifacts_index.json`` the audit asserts
   that no pitch in the study's holdout set appears in that artifact's
   recorded ``training_pitch_ids``. A single overlapping pitch raises
   :class:`LeakageDetectedError` (the operator-greppable error code is
   ``LEAKAGE_DETECTED``).
2. **Strict temporal pre/post-2020 split.** The audit calls
   :func:`temporal_split.split` with the pinned defaults
   (train_end="2019-12-31", buffer_year=2020, holdout_start=
   "2021-01-01") and refuses the run if any holdout row falls outside
   the post-buffer window. Operators may override the buffer year only
   via an explicit ``buffer_override`` block in the audit config and
   only after writing a justification into the validation
   pre-registration YAML — see ``docs/specs/leakage_audit.md``.
3. **Distribution stability.** Per-feature, the training and holdout
   marginals are compared with a two-sample Kolmogorov-Smirnov
   statistic and a Population Stability Index (PSI). PSI ≥ 0.25 is
   warned; PSI ≥ 0.50 escalates to an error. A drift error is
   ``LEAKAGE_DETECTED`` because a hard distribution shift is
   indistinguishable, downstream, from a leak that smuggled the wrong
   population into the holdout.

The audit is pure stdlib (no numpy / scipy) so it can run in the
project's baseline environment. Same inputs ⇒ byte-identical
:class:`LeakageReport` payload.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from coherence_engine.server.fund.services import temporal_split as ts


LEAKAGE_AUDIT_SCHEMA_VERSION = "leakage-audit-report-v1"

LEAKAGE_DETECTED = "LEAKAGE_DETECTED"

# PSI thresholds. Standard credit-risk practice:
#   PSI <  0.10  : no significant shift
#   0.10 <= PSI < 0.25 : moderate shift (informational)
#   0.25 <= PSI < 0.50 : warn
#   PSI >= 0.50  : error
_PSI_WARN = 0.25
_PSI_ERROR = 0.50

# KS-test thresholds. We keep the rule-of-thumb 1.36 / sqrt(harmonic_n)
# critical value for alpha=0.05 (two-sided), implemented inline.
_KS_ALPHA = 0.05
_KS_CRITICAL_FACTOR = 1.36

# Default histogram bin count for PSI.
_PSI_BINS = 10

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[3]
DEFAULT_TRAINING_INDEX_PATH = (
    _REPO_ROOT / "data" / "governed" / "training_artifacts_index.json"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LeakageAuditError(RuntimeError):
    """Base class for leakage-audit failures."""


class LeakageDetectedError(LeakageAuditError):
    """Raised when any audit assertion fails.

    The error's ``code`` attribute is always ``LEAKAGE_DETECTED`` so
    operator tooling can grep for the marker without depending on the
    exact human-readable message.
    """

    code = LEAKAGE_DETECTED

    def __init__(self, message: str, *, report: "LeakageReport"):
        self.report = report
        super().__init__(f"{LEAKAGE_DETECTED}: {message}")


class TrainingArtifactsIndexError(LeakageAuditError):
    """Raised when the training-artifacts index file is missing or malformed."""


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureDriftResult:
    """Per-feature drift summary."""

    feature: str
    n_train: int
    n_holdout: int
    ks_statistic: float
    ks_critical: float
    ks_alarm: bool
    psi: float
    psi_alarm: str  # "ok" | "warn" | "error"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.feature,
            "ks_alarm": bool(self.ks_alarm),
            "ks_critical": round(self.ks_critical, 6),
            "ks_statistic": round(self.ks_statistic, 6),
            "n_holdout": int(self.n_holdout),
            "n_train": int(self.n_train),
            "psi": round(self.psi, 6),
            "psi_alarm": self.psi_alarm,
        }


@dataclass(frozen=True)
class ArtifactMembershipResult:
    """Per-artifact membership-overlap summary."""

    artifact_id: str
    kind: str
    training_set_hash: str
    n_training_pitches: int
    overlapping_pitch_ids: Tuple[str, ...]
    overlap_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "n_training_pitches": int(self.n_training_pitches),
            "overlap_count": int(self.overlap_count),
            "overlapping_pitch_ids": list(self.overlapping_pitch_ids),
            "training_set_hash": self.training_set_hash,
        }


@dataclass(frozen=True)
class TemporalSplitSummary:
    """Result of the temporal split evaluated against the audit corpus."""

    train_end_year: int
    buffer_year: int
    holdout_start_year: int
    n_train: int
    n_holdout: int
    n_buffer_excluded: int
    n_undated_excluded: int
    holdout_outside_window_pitch_ids: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "buffer_year": int(self.buffer_year),
            "holdout_outside_window_pitch_ids": list(
                self.holdout_outside_window_pitch_ids
            ),
            "holdout_start_year": int(self.holdout_start_year),
            "n_buffer_excluded": int(self.n_buffer_excluded),
            "n_holdout": int(self.n_holdout),
            "n_train": int(self.n_train),
            "n_undated_excluded": int(self.n_undated_excluded),
            "train_end_year": int(self.train_end_year),
        }


@dataclass(frozen=True)
class LeakageReport:
    """Structured audit report.

    ``passed`` is the single load-bearing field: when it is ``False``
    the validation study harness MUST refuse to render its final report
    (see ``validation_study._enforce_leakage_audit``).
    """

    schema_version: str
    passed: bool
    failed_assertions: Tuple[str, ...]
    warnings: Tuple[str, ...]
    artifact_membership: Tuple[ArtifactMembershipResult, ...]
    feature_drift: Tuple[FeatureDriftResult, ...]
    temporal_split: TemporalSplitSummary
    config: Dict[str, Any]
    audit_digest: str = ""

    def to_dict(self) -> Dict[str, Any]:
        body = {
            "artifact_membership": [a.to_dict() for a in self.artifact_membership],
            "config": self.config,
            "failed_assertions": list(self.failed_assertions),
            "feature_drift": [f.to_dict() for f in self.feature_drift],
            "passed": bool(self.passed),
            "schema_version": self.schema_version,
            "temporal_split": self.temporal_split.to_dict(),
            "warnings": list(self.warnings),
        }
        return body

    def to_canonical_bytes(self) -> bytes:
        body = self.to_dict()
        # the audit_digest is the hash of body *without* its own field,
        # so we only emit it as a separate envelope key on the wire.
        body["audit_digest"] = self.audit_digest
        return (
            json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Training artifacts index
# ---------------------------------------------------------------------------


def load_training_artifacts_index(
    path: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    """Load and minimally validate the training-artifacts index.

    The required top-level shape is ``{"training_artifacts": [ ... ]}``.
    Each artifact must carry ``artifact_id`` and ``training_pitch_ids``
    (a list of strings; may be empty for an as-yet-unfit artifact).
    """

    target = Path(path) if path else DEFAULT_TRAINING_INDEX_PATH
    if not target.is_file():
        raise TrainingArtifactsIndexError(
            f"training-artifacts index not found: {target}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrainingArtifactsIndexError(
            f"training-artifacts index is not valid JSON: {target}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise TrainingArtifactsIndexError(
            f"training-artifacts index must be a JSON object: {target}"
        )
    arts = payload.get("training_artifacts")
    if not isinstance(arts, list):
        raise TrainingArtifactsIndexError(
            "training-artifacts index missing 'training_artifacts' list"
        )
    for i, a in enumerate(arts):
        if not isinstance(a, Mapping):
            raise TrainingArtifactsIndexError(
                f"training_artifacts[{i}] must be a JSON object"
            )
        if "artifact_id" not in a:
            raise TrainingArtifactsIndexError(
                f"training_artifacts[{i}] missing 'artifact_id'"
            )
        tps = a.get("training_pitch_ids")
        if tps is None or not isinstance(tps, list):
            raise TrainingArtifactsIndexError(
                f"training_artifacts[{i}].training_pitch_ids must be a list"
            )
    return dict(payload)


# ---------------------------------------------------------------------------
# Pure-stdlib statistics
# ---------------------------------------------------------------------------


def ks_two_sample(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float, bool]:
    """Two-sample Kolmogorov-Smirnov statistic + critical value.

    Returns ``(statistic, critical_value, alarm)``. ``alarm`` is ``True``
    when the statistic exceeds the alpha=0.05 critical value
    ``1.36 * sqrt((n1 + n2) / (n1 * n2))``. Empty inputs yield
    ``(0.0, inf, False)`` so the audit treats unmeasurable features as
    non-alarming.
    """

    n1 = len(a)
    n2 = len(b)
    if n1 == 0 or n2 == 0:
        return 0.0, float("inf"), False
    sa = sorted(a)
    sb = sorted(b)
    i = j = 0
    cdf_a = cdf_b = 0.0
    d = 0.0
    while i < n1 and j < n2:
        if sa[i] < sb[j]:
            i += 1
            cdf_a = i / n1
        elif sa[i] > sb[j]:
            j += 1
            cdf_b = j / n2
        else:
            # tie: advance both ECDFs by one step at the same x value
            v = sa[i]
            while i < n1 and sa[i] == v:
                i += 1
            while j < n2 and sb[j] == v:
                j += 1
            cdf_a = i / n1
            cdf_b = j / n2
        if abs(cdf_a - cdf_b) > d:
            d = abs(cdf_a - cdf_b)
    # tail
    while i < n1:
        i += 1
        cdf_a = i / n1
        if abs(cdf_a - cdf_b) > d:
            d = abs(cdf_a - cdf_b)
    while j < n2:
        j += 1
        cdf_b = j / n2
        if abs(cdf_a - cdf_b) > d:
            d = abs(cdf_a - cdf_b)
    critical = _KS_CRITICAL_FACTOR * math.sqrt((n1 + n2) / (n1 * n2))
    return d, critical, d > critical


def population_stability_index(
    train: Sequence[float],
    holdout: Sequence[float],
    *,
    n_bins: int = _PSI_BINS,
) -> float:
    """Compute PSI between ``train`` and ``holdout`` with quantile bins.

    Bins are derived from the *training* distribution's quantile cuts so
    a holdout marked as "drifted" is shifted relative to the population
    the artifact was fit on (the asymmetry that matters here). Empty
    bins are floored at a tiny epsilon so the log term stays finite.
    """

    if not train or not holdout:
        return 0.0
    sorted_train = sorted(train)
    n = len(sorted_train)
    # build edges from training quantiles
    edges: List[float] = []
    for k in range(1, n_bins):
        # quantile k/n_bins of sorted_train via linear interp
        pos = (k / n_bins) * (n - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            q = sorted_train[lo]
        else:
            frac = pos - lo
            q = sorted_train[lo] * (1 - frac) + sorted_train[hi] * frac
        edges.append(q)
    # collapse duplicate edges so an extremely peaked distribution
    # cannot blow up the bin count
    dedup_edges = sorted(set(edges))

    def _bin(values: Sequence[float]) -> List[float]:
        counts = [0] * (len(dedup_edges) + 1)
        for v in values:
            placed = False
            for k, edge in enumerate(dedup_edges):
                if v <= edge:
                    counts[k] += 1
                    placed = True
                    break
            if not placed:
                counts[-1] += 1
        total = sum(counts)
        if total == 0:
            return [0.0] * len(counts)
        return [c / total for c in counts]

    p = _bin(sorted_train)
    q = _bin(sorted(holdout))
    eps = 1e-6
    psi = 0.0
    for pi, qi in zip(p, q, strict=True):
        pi_eff = max(pi, eps)
        qi_eff = max(qi, eps)
        psi += (pi_eff - qi_eff) * math.log(pi_eff / qi_eff)
    return psi


def _psi_alarm_level(psi: float) -> str:
    if psi >= _PSI_ERROR:
        return "error"
    if psi >= _PSI_WARN:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# Audit config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditConfig:
    """Inputs to a single :func:`audit` invocation.

    The audit deliberately operates on plain dicts (not StudyRow) so it
    can be called from a CLI ``leakage audit`` command without first
    constructing the full study harness.
    """

    corpus: Tuple[Mapping[str, Any], ...] = ()
    holdout_pitch_ids: Optional[Tuple[str, ...]] = None
    feature_extractors: Tuple[str, ...] = ()
    training_artifacts_index_path: Optional[Path] = None
    train_end: Any = "2019-12-31"
    buffer_year: int = 2020
    holdout_start: Any = "2021-01-01"
    buffer_override_rationale: Optional[str] = None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _coerce_features(
    rows: Iterable[Mapping[str, Any]],
    feature: str,
) -> List[float]:
    out: List[float] = []
    for r in rows:
        v = r.get(feature)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def audit(study_config: AuditConfig | Mapping[str, Any]) -> LeakageReport:
    """Run the full leakage audit and return a :class:`LeakageReport`.

    ``study_config`` may be an :class:`AuditConfig` or a plain mapping
    with the same keys (the latter is convenient for CLI / JSON
    callers). The report is *always* returned — callers inspect
    ``report.passed`` and call :func:`enforce` if they want the audit
    to raise ``LeakageDetectedError`` on failure.
    """

    cfg = _normalize_config(study_config)

    # 1) temporal split
    split_result = ts.split(
        cfg.corpus,
        train_end=cfg.train_end,
        buffer_year=cfg.buffer_year,
        holdout_start=cfg.holdout_start,
    )

    declared_holdout_ids: Optional[set] = None
    if cfg.holdout_pitch_ids is not None:
        declared_holdout_ids = {str(p) for p in cfg.holdout_pitch_ids}

    inferred_holdout_ids = {
        str(r.get("pitch_id"))
        for r in split_result.holdout
        if r.get("pitch_id") is not None
    }
    # If the caller declared an explicit holdout set, it must be a
    # subset of the post-buffer window. Anything outside is a hard
    # window violation.
    holdout_outside_window: Tuple[str, ...] = ()
    if declared_holdout_ids is not None:
        outside = sorted(declared_holdout_ids - inferred_holdout_ids)
        holdout_outside_window = tuple(outside)
        # Use the declared holdout for membership checks below
        holdout_for_audit = declared_holdout_ids
    else:
        holdout_for_audit = inferred_holdout_ids

    failed: List[str] = []
    warnings: List[str] = []

    if holdout_outside_window:
        failed.append(
            f"temporal_split: {len(holdout_outside_window)} declared holdout "
            f"pitch_id(s) fall outside the post-buffer window "
            f"(holdout_start_year={split_result.config.holdout_start_year})"
        )

    # Buffer-override rationale: the prompt 45 contract requires an
    # explicit operator override + a written rationale to shrink the
    # buffer year. The audit accepts the rationale field (the YAML side
    # of the override is enforced at the validation_study layer).
    if cfg.buffer_year != 2020 and not cfg.buffer_override_rationale:
        failed.append(
            "buffer_year override requested without buffer_override_rationale; "
            "shrinking the buffer requires a written rationale per prompt 45"
        )

    # 2) artifact membership
    arts_payload = load_training_artifacts_index(cfg.training_artifacts_index_path)
    membership: List[ArtifactMembershipResult] = []
    for raw in arts_payload["training_artifacts"]:
        tpids = [str(p) for p in raw.get("training_pitch_ids", [])]
        train_set = set(tpids)
        overlap = sorted(holdout_for_audit & train_set)
        membership.append(
            ArtifactMembershipResult(
                artifact_id=str(raw.get("artifact_id")),
                kind=str(raw.get("kind") or ""),
                training_set_hash=str(raw.get("training_set_hash") or ""),
                n_training_pitches=len(tpids),
                overlapping_pitch_ids=tuple(overlap),
                overlap_count=len(overlap),
            )
        )
        if overlap:
            failed.append(
                f"artifact_membership: {len(overlap)} holdout pitch(es) appear "
                f"in training set of artifact {raw.get('artifact_id')!r}"
            )
        # extra check: if the artifact lists a hash, recompute the hash
        # over the sorted training_pitch_ids and warn on mismatch. This
        # catches operator drift between the index and the artifact.
        declared_hash = str(raw.get("training_set_hash") or "")
        if declared_hash and declared_hash != "PENDING_FIRST_FIT":
            recomputed = hashlib.sha256(
                ("\n".join(sorted(tpids))).encode("utf-8")
            ).hexdigest()
            if recomputed != declared_hash:
                warnings.append(
                    f"artifact_membership: declared training_set_hash for "
                    f"{raw.get('artifact_id')!r} does not match recomputed "
                    f"hash of its training_pitch_ids"
                )

    # 3) distribution audit
    train_rows = list(split_result.train)
    if declared_holdout_ids is not None:
        holdout_rows = [
            r for r in cfg.corpus
            if str(r.get("pitch_id")) in declared_holdout_ids
        ]
    else:
        holdout_rows = list(split_result.holdout)

    drift: List[FeatureDriftResult] = []
    for feature in cfg.feature_extractors:
        a = _coerce_features(train_rows, feature)
        b = _coerce_features(holdout_rows, feature)
        ks_stat, ks_crit, ks_alarm = ks_two_sample(a, b)
        psi = population_stability_index(a, b)
        psi_level = _psi_alarm_level(psi)
        drift.append(
            FeatureDriftResult(
                feature=feature,
                n_train=len(a),
                n_holdout=len(b),
                ks_statistic=ks_stat,
                ks_critical=ks_crit,
                ks_alarm=ks_alarm,
                psi=psi,
                psi_alarm=psi_level,
            )
        )
        if psi_level == "error":
            failed.append(
                f"distribution_drift: feature {feature!r} PSI={psi:.3f} >= "
                f"{_PSI_ERROR} (training/holdout marginals diverge severely)"
            )
        elif psi_level == "warn":
            warnings.append(
                f"distribution_drift: feature {feature!r} PSI={psi:.3f} in "
                f"warn band [{_PSI_WARN}, {_PSI_ERROR})"
            )
        if ks_alarm:
            warnings.append(
                f"distribution_drift: feature {feature!r} KS={ks_stat:.3f} "
                f"exceeds alpha={_KS_ALPHA} critical {ks_crit:.3f}"
            )

    summary = TemporalSplitSummary(
        train_end_year=split_result.config.train_end_year,
        buffer_year=split_result.config.buffer_year,
        holdout_start_year=split_result.config.holdout_start_year,
        n_train=len(split_result.train),
        n_holdout=len(split_result.holdout),
        n_buffer_excluded=len(split_result.buffer_excluded),
        n_undated_excluded=len(split_result.undated_excluded),
        holdout_outside_window_pitch_ids=holdout_outside_window,
    )

    config_block = {
        "buffer_override_rationale": cfg.buffer_override_rationale,
        "buffer_year": int(cfg.buffer_year),
        "feature_extractors": list(cfg.feature_extractors),
        "holdout_start": _stringify(cfg.holdout_start),
        "n_corpus_rows": len(cfg.corpus),
        "n_declared_holdout_pitch_ids": (
            len(cfg.holdout_pitch_ids) if cfg.holdout_pitch_ids is not None else None
        ),
        "train_end": _stringify(cfg.train_end),
        "training_artifacts_index_path": (
            str(Path(cfg.training_artifacts_index_path).resolve())
            if cfg.training_artifacts_index_path
            else str(DEFAULT_TRAINING_INDEX_PATH)
        ),
    }

    passed = not failed
    report = LeakageReport(
        schema_version=LEAKAGE_AUDIT_SCHEMA_VERSION,
        passed=passed,
        failed_assertions=tuple(failed),
        warnings=tuple(warnings),
        artifact_membership=tuple(membership),
        feature_drift=tuple(drift),
        temporal_split=summary,
        config=config_block,
    )
    digest = hashlib.sha256(
        json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report = LeakageReport(
        schema_version=report.schema_version,
        passed=report.passed,
        failed_assertions=report.failed_assertions,
        warnings=report.warnings,
        artifact_membership=report.artifact_membership,
        feature_drift=report.feature_drift,
        temporal_split=report.temporal_split,
        config=report.config,
        audit_digest=digest,
    )
    return report


def enforce(report: LeakageReport) -> None:
    """Raise :class:`LeakageDetectedError` if the report did not pass.

    Used by the validation-study renderer (``run_study``) and the
    ``leakage audit`` CLI verb. Returns ``None`` quietly on a passing
    report.
    """

    if report.passed:
        return
    msg = "; ".join(report.failed_assertions) or "audit failed"
    raise LeakageDetectedError(msg, report=report)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> Any:
    if isinstance(value, (int, str)) or value is None:
        return value
    return str(value)


def _normalize_config(cfg: AuditConfig | Mapping[str, Any]) -> AuditConfig:
    if isinstance(cfg, AuditConfig):
        return cfg
    if not isinstance(cfg, Mapping):
        raise LeakageAuditError(
            f"study_config must be AuditConfig or Mapping; got {type(cfg).__name__}"
        )
    return AuditConfig(
        corpus=tuple(cfg.get("corpus") or ()),
        holdout_pitch_ids=(
            tuple(str(p) for p in cfg["holdout_pitch_ids"])
            if cfg.get("holdout_pitch_ids") is not None
            else None
        ),
        feature_extractors=tuple(cfg.get("feature_extractors") or ()),
        training_artifacts_index_path=(
            Path(cfg["training_artifacts_index_path"])
            if cfg.get("training_artifacts_index_path")
            else None
        ),
        train_end=cfg.get("train_end", "2019-12-31"),
        buffer_year=int(cfg.get("buffer_year", 2020)),
        holdout_start=cfg.get("holdout_start", "2021-01-01"),
        buffer_override_rationale=cfg.get("buffer_override_rationale"),
    )


__all__ = [
    "AuditConfig",
    "ArtifactMembershipResult",
    "DEFAULT_TRAINING_INDEX_PATH",
    "FeatureDriftResult",
    "LEAKAGE_AUDIT_SCHEMA_VERSION",
    "LEAKAGE_DETECTED",
    "LeakageAuditError",
    "LeakageDetectedError",
    "LeakageReport",
    "TemporalSplitSummary",
    "TrainingArtifactsIndexError",
    "audit",
    "enforce",
    "ks_two_sample",
    "load_training_artifacts_index",
    "population_stability_index",
]
