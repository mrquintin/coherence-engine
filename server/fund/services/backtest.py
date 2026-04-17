"""Offline, deterministic backtest pipeline for the governed historical
outcomes dataset.

This service replays a governed-format JSONL dataset (same row shape as
``data/governed/uncertainty_historical_outcomes.jsonl``) through the
**current** production scorer + decision policy, using a **fixed**
``PortfolioSnapshot`` loaded from disk so the result is reproducible
regardless of any live database state.

Key invariants (see ``docs/specs/backtest_spec.md``):

* No network calls.
* No reads from the live portfolio_state / positions tables — the
  snapshot is supplied via ``BacktestConfig.portfolio_snapshot_path``.
* Output JSON is byte-identical when the same ``BacktestConfig`` is run
  twice over the same dataset (``json.dumps(..., sort_keys=True,
  separators=(",", ":"))``).
* Wall-clock is never read; only the dataset and the snapshot file are
  consulted.

The backtest reports per-row decision verdicts plus aggregate metrics:
verdict counts (pass / reject / manual_review), Brier score on a
``outcome_superiority > 0`` binary realization, a 10-bin reliability
curve, mean realized vs. predicted superiority delta, and a
domain-level breakdown.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from coherence_engine.server.fund.services.decision_policy import (
    DECISION_POLICY_VERSION,
    DecisionPolicyService,
    PortfolioSnapshot,
)
from coherence_engine.server.fund.services.governed_historical_dataset import (
    HistoricalOutcomesExportValidation,
    validate_historical_outcomes_export,
)
from coherence_engine.server.fund.services.uncertainty import (
    UNCERTAINTY_MODEL_VERSION,
    calibrated_superiority_interval_95,
)
from coherence_engine.server.fund.services.uncertainty_calibration import (
    load_historical_records,
    to_governed_jsonl_record,
)


BACKTEST_SCHEMA_VERSION = "backtest-report-v1"

_DEFAULT_DOMAIN = "market_economics"
_DEFAULT_REQUESTED_USD_DEFAULT = 50_000.0
_DEFAULT_TRANSCRIPT_QUALITY_FLOOR = 0.85
_RELIABILITY_BIN_COUNT = 10


# ---------------------------------------------------------------------------
# Config / report dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestConfig:
    """Inputs for a single deterministic backtest run.

    Attributes:
        dataset_path: Path to a governed-format JSONL (or JSON array)
            historical-outcomes file. Must validate via
            :func:`validate_historical_outcomes_export`.
        decision_policy_version: Policy version pin used to assert that
            the running ``DECISION_POLICY_VERSION`` matches the value the
            operator intended to backtest. Validated at run time;
            mismatch raises ``BacktestError``.
        portfolio_snapshot_path: Path to a JSON file describing a single
            :class:`PortfolioSnapshot`. The snapshot is used uniformly
            for **every** row — the live portfolio state is never read.
        output_path: Where the final report JSON is written. ``None``
            means "do not write to disk" (the in-memory
            :class:`BacktestReport` is still returned).
        seed: Reserved for future stochastic extensions; currently
            unused so present runs are pure-deterministic. Recorded in
            the report for reproducibility.
        requested_check_usd: Optional override for the per-row
            requested check size. Defaults to a small fixed value so
            portfolio-gate behavior matches the historical
            "single-check" spirit of the seed dataset.
        domain_default: Domain key used for rows that omit one
            (the governed seed dataset historically does).
    """

    dataset_path: Path
    decision_policy_version: str = DECISION_POLICY_VERSION
    portfolio_snapshot_path: Optional[Path] = None
    output_path: Optional[Path] = None
    seed: int = 0
    requested_check_usd: float = _DEFAULT_REQUESTED_USD_DEFAULT
    domain_default: str = _DEFAULT_DOMAIN


@dataclass(frozen=True)
class BacktestRowResult:
    """Per-row outcome of a single backtest replay.

    All fields are JSON-serializable primitives so the row can appear
    verbatim in the deterministic report.
    """

    index: int
    domain: str
    coherence_superiority: float
    ci_lower: float
    ci_upper: float
    predicted_probability: float
    realized_outcome: int
    realized_superiority: float
    decision: str
    threshold_required: float
    margin: float


@dataclass(frozen=True)
class ReliabilityBin:
    bin_index: int
    bin_lower: float
    bin_upper: float
    count: int
    mean_predicted: float
    mean_realized: float


@dataclass(frozen=True)
class BacktestReport:
    """Aggregated backtest result.

    Use :meth:`to_canonical_dict` to obtain a sort-keyed dict ready for
    deterministic JSON serialization, and :meth:`to_canonical_bytes`
    for the byte-identical artifact that is written to disk.
    """

    schema_version: str
    generated_with: Dict[str, str]
    config: Dict[str, Any]
    n_rows: int
    n_skipped: int
    pass_count: int
    reject_count: int
    manual_review_count: int
    pass_rate: float
    reject_rate: float
    manual_review_rate: float
    realized_positive_rate: float
    mean_predicted_probability: float
    mean_predicted_minus_realized: float
    brier_score: float
    reliability_curve: Tuple[ReliabilityBin, ...]
    domain_breakdown: Dict[str, Dict[str, float]]
    rows: Tuple[BacktestRowResult, ...] = field(default_factory=tuple)

    def to_canonical_dict(self) -> Dict[str, Any]:
        return _canonical_report_dict(self)

    def to_canonical_bytes(self) -> bytes:
        payload = self.to_canonical_dict()
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

    def report_digest(self) -> str:
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()


class BacktestError(RuntimeError):
    """Raised on validation failures or unrecoverable backtest errors."""


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------


def load_portfolio_snapshot(path: Optional[Path]) -> PortfolioSnapshot:
    """Read a fixed :class:`PortfolioSnapshot` from disk.

    A ``None`` path returns a default snapshot that produces no
    portfolio adjustments (zeros + ``"normal"`` regime). The file
    schema is intentionally permissive: any keys that match
    ``PortfolioSnapshot`` field names are honored, all others are
    ignored. The snapshot is intentionally read-only and never written
    back.
    """
    if path is None:
        return PortfolioSnapshot()
    p = Path(path)
    if not p.is_file():
        raise BacktestError(f"portfolio_snapshot file not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BacktestError(f"portfolio_snapshot is not valid JSON: {p}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise BacktestError(f"portfolio_snapshot must be a JSON object: {p}")

    domain_invested = raw.get("domain_invested_usd") or {}
    if not isinstance(domain_invested, Mapping):
        raise BacktestError("portfolio_snapshot.domain_invested_usd must be an object")
    return PortfolioSnapshot(
        fund_nav_usd=float(raw.get("fund_nav_usd", 0.0) or 0.0),
        liquidity_reserve_usd=float(raw.get("liquidity_reserve_usd", 0.0) or 0.0),
        drawdown_proxy=max(0.0, min(1.0, float(raw.get("drawdown_proxy", 0.0) or 0.0))),
        regime=str(raw.get("regime", "normal") or "normal"),
        domain_invested_usd={str(k): float(v or 0.0) for k, v in domain_invested.items()},
        as_of=None,
    )


# ---------------------------------------------------------------------------
# Per-row replay
# ---------------------------------------------------------------------------


def _predicted_probability(superiority: float) -> float:
    """Map ``coherence_superiority ∈ [-1, 1]`` to a binary "good outcome"
    probability in ``[0, 1]`` via a clamped affine transform.

    The mapping is intentionally simple and monotonic: an exotic
    calibrator would couple this to the uncertainty interval and risk
    making the backtest non-portable across calibration profile
    revisions. See ``docs/specs/backtest_spec.md`` for the rationale.
    """
    p = 0.5 + 0.5 * float(superiority)
    if p < 0.0:
        return 0.0
    if p > 1.0:
        return 1.0
    return p


def _realized_outcome(outcome_superiority: float) -> int:
    """Realized binary outcome: 1 if outcome was strictly positive, else 0."""
    return 1 if float(outcome_superiority) > 0.0 else 0


def _build_application_for_row(
    row: Mapping[str, Any],
    *,
    requested_check_usd: float,
    domain_default: str,
) -> Dict[str, Any]:
    domain = str(row.get("domain", domain_default) or domain_default)
    return {
        "domain_primary": domain,
        "requested_check_usd": int(requested_check_usd),
        "compliance_status": str(row.get("compliance_status", "clear") or "clear"),
    }


def _build_score_record(
    row: Mapping[str, Any],
    *,
    ci_lower: float,
    ci_upper: float,
) -> Dict[str, Any]:
    transcript_q = float(row.get("transcript_quality", 1.0) or 1.0)
    transcript_q = max(_DEFAULT_TRANSCRIPT_QUALITY_FLOOR, min(1.0, transcript_q))
    anti_gaming = float(row.get("anti_gaming_score", 1.0) or 1.0)
    return {
        "transcript_quality_score": transcript_q,
        "anti_gaming_score": anti_gaming,
        "coherence_superiority_ci95": {"lower": float(ci_lower), "upper": float(ci_upper)},
    }


def _replay_row(
    idx: int,
    row: Mapping[str, Any],
    *,
    config: BacktestConfig,
    snapshot: PortfolioSnapshot,
    policy: DecisionPolicyService,
) -> Optional[BacktestRowResult]:
    governed = to_governed_jsonl_record(row)
    if governed is None:
        return None

    cs = float(governed["coherence_superiority"])
    outcome = float(governed["outcome_superiority"])
    n_props = int(governed["n_propositions"])
    transcript_quality = float(governed["transcript_quality"])
    n_contradictions = int(governed["n_contradictions"])
    layer_scores = dict(governed["layer_scores"])

    lo, hi, _ = calibrated_superiority_interval_95(
        superiority=cs,
        n_propositions=n_props,
        transcript_quality=transcript_quality,
        n_contradictions=n_contradictions,
        layer_scores=layer_scores,
    )

    application = _build_application_for_row(
        row,
        requested_check_usd=config.requested_check_usd,
        domain_default=config.domain_default,
    )
    score_record = _build_score_record(row, ci_lower=lo, ci_upper=hi)

    decision_dict = policy.evaluate(
        application,
        score_record,
        portfolio_snapshot=snapshot,
    )

    return BacktestRowResult(
        index=idx,
        domain=str(application["domain_primary"]),
        coherence_superiority=round(cs, 6),
        ci_lower=round(float(lo), 6),
        ci_upper=round(float(hi), 6),
        predicted_probability=round(_predicted_probability(cs), 6),
        realized_outcome=_realized_outcome(outcome),
        realized_superiority=round(outcome, 6),
        decision=str(decision_dict["decision"]),
        threshold_required=round(float(decision_dict["threshold_required"]), 6),
        margin=round(float(decision_dict["margin"]), 6),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _reliability_curve(
    rows: Sequence[BacktestRowResult],
    *,
    n_bins: int = _RELIABILITY_BIN_COUNT,
) -> Tuple[ReliabilityBin, ...]:
    """Equal-width reliability bins over the predicted-probability axis.

    Bin edges are ``[k/n_bins, (k+1)/n_bins)`` except the last bin is
    closed on the right so a predicted_probability of exactly 1.0 lands
    in the final bin. Empty bins are emitted with zero count and zero
    means so the report shape is stable across datasets.
    """
    if n_bins <= 0:
        return ()
    buckets: List[List[BacktestRowResult]] = [[] for _ in range(n_bins)]
    for r in rows:
        p = max(0.0, min(1.0, r.predicted_probability))
        if p >= 1.0:
            idx = n_bins - 1
        else:
            idx = min(n_bins - 1, int(math.floor(p * n_bins)))
        buckets[idx].append(r)

    out: List[ReliabilityBin] = []
    for k, bucket in enumerate(buckets):
        lo = k / n_bins
        hi = (k + 1) / n_bins
        if not bucket:
            out.append(
                ReliabilityBin(
                    bin_index=k,
                    bin_lower=round(lo, 6),
                    bin_upper=round(hi, 6),
                    count=0,
                    mean_predicted=0.0,
                    mean_realized=0.0,
                )
            )
            continue
        mean_pred = sum(b.predicted_probability for b in bucket) / len(bucket)
        mean_real = sum(b.realized_outcome for b in bucket) / len(bucket)
        out.append(
            ReliabilityBin(
                bin_index=k,
                bin_lower=round(lo, 6),
                bin_upper=round(hi, 6),
                count=len(bucket),
                mean_predicted=round(mean_pred, 6),
                mean_realized=round(mean_real, 6),
            )
        )
    return tuple(out)


def _domain_breakdown(
    rows: Sequence[BacktestRowResult],
) -> Dict[str, Dict[str, float]]:
    by_domain: Dict[str, List[BacktestRowResult]] = {}
    for r in rows:
        by_domain.setdefault(r.domain, []).append(r)
    out: Dict[str, Dict[str, float]] = {}
    for domain, bucket in sorted(by_domain.items()):
        n = len(bucket)
        if n == 0:  # pragma: no cover - defensive
            continue
        passes = sum(1 for r in bucket if r.decision == "pass")
        rejects = sum(1 for r in bucket if r.decision == "fail")
        mreviews = sum(1 for r in bucket if r.decision == "manual_review")
        positives = sum(r.realized_outcome for r in bucket)
        brier = sum((r.predicted_probability - r.realized_outcome) ** 2 for r in bucket) / n
        out[domain] = {
            "n": float(n),
            "pass_rate": round(passes / n, 6),
            "reject_rate": round(rejects / n, 6),
            "manual_review_rate": round(mreviews / n, 6),
            "realized_positive_rate": round(positives / n, 6),
            "brier_score": round(brier, 6),
        }
    return out


def _aggregate(rows: Sequence[BacktestRowResult]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "pass_count": 0,
            "reject_count": 0,
            "manual_review_count": 0,
            "pass_rate": 0.0,
            "reject_rate": 0.0,
            "manual_review_rate": 0.0,
            "realized_positive_rate": 0.0,
            "mean_predicted_probability": 0.0,
            "mean_predicted_minus_realized": 0.0,
            "brier_score": 0.0,
        }
    passes = sum(1 for r in rows if r.decision == "pass")
    rejects = sum(1 for r in rows if r.decision == "fail")
    mreviews = sum(1 for r in rows if r.decision == "manual_review")
    positives = sum(r.realized_outcome for r in rows)
    mean_pred = sum(r.predicted_probability for r in rows) / n
    mean_real = positives / n
    brier = sum((r.predicted_probability - r.realized_outcome) ** 2 for r in rows) / n
    return {
        "pass_count": passes,
        "reject_count": rejects,
        "manual_review_count": mreviews,
        "pass_rate": round(passes / n, 6),
        "reject_rate": round(rejects / n, 6),
        "manual_review_rate": round(mreviews / n, 6),
        "realized_positive_rate": round(mean_real, 6),
        "mean_predicted_probability": round(mean_pred, 6),
        "mean_predicted_minus_realized": round(mean_pred - mean_real, 6),
        "brier_score": round(brier, 6),
    }


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def _canonical_row(r: BacktestRowResult) -> Dict[str, Any]:
    return {
        "ci_lower": r.ci_lower,
        "ci_upper": r.ci_upper,
        "coherence_superiority": r.coherence_superiority,
        "decision": r.decision,
        "domain": r.domain,
        "index": r.index,
        "margin": r.margin,
        "predicted_probability": r.predicted_probability,
        "realized_outcome": r.realized_outcome,
        "realized_superiority": r.realized_superiority,
        "threshold_required": r.threshold_required,
    }


def _canonical_bin(b: ReliabilityBin) -> Dict[str, Any]:
    return {
        "bin_index": b.bin_index,
        "bin_lower": b.bin_lower,
        "bin_upper": b.bin_upper,
        "count": b.count,
        "mean_predicted": b.mean_predicted,
        "mean_realized": b.mean_realized,
    }


def _canonical_report_dict(report: BacktestReport) -> Dict[str, Any]:
    return {
        "aggregates": {
            "brier_score": report.brier_score,
            "manual_review_count": report.manual_review_count,
            "manual_review_rate": report.manual_review_rate,
            "mean_predicted_minus_realized": report.mean_predicted_minus_realized,
            "mean_predicted_probability": report.mean_predicted_probability,
            "n_rows": report.n_rows,
            "n_skipped": report.n_skipped,
            "pass_count": report.pass_count,
            "pass_rate": report.pass_rate,
            "realized_positive_rate": report.realized_positive_rate,
            "reject_count": report.reject_count,
            "reject_rate": report.reject_rate,
        },
        "config": report.config,
        "domain_breakdown": report.domain_breakdown,
        "generated_with": report.generated_with,
        "reliability_curve": [_canonical_bin(b) for b in report.reliability_curve],
        "rows": [_canonical_row(r) for r in report.rows],
        "schema_version": report.schema_version,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _validate_dataset(path: Path) -> HistoricalOutcomesExportValidation:
    """Validate the dataset file via the shared historical-export validator."""
    return validate_historical_outcomes_export(path, require_standard_layer_keys=False)


def _config_audit(config: BacktestConfig) -> Dict[str, Any]:
    """Render ``config`` into a deterministic, JSON-safe dict for the report."""
    return {
        "dataset_path": str(Path(config.dataset_path).resolve()),
        "decision_policy_version": str(config.decision_policy_version),
        "domain_default": str(config.domain_default),
        "output_path": str(Path(config.output_path).resolve()) if config.output_path else None,
        "portfolio_snapshot_path": (
            str(Path(config.portfolio_snapshot_path).resolve())
            if config.portfolio_snapshot_path
            else None
        ),
        "requested_check_usd": float(config.requested_check_usd),
        "seed": int(config.seed),
    }


def run_backtest(config: BacktestConfig) -> BacktestReport:
    """Run the deterministic backtest.

    Raises :class:`BacktestError` if the dataset fails the historical-
    export validator, if the configured policy version does not match
    the running code, or if the snapshot file is unreadable.
    """
    if str(config.decision_policy_version) != DECISION_POLICY_VERSION:
        raise BacktestError(
            "decision_policy_version pin does not match running policy: "
            f"requested={config.decision_policy_version!r} "
            f"actual={DECISION_POLICY_VERSION!r}"
        )

    dataset_path = Path(config.dataset_path)
    if not dataset_path.is_file():
        raise BacktestError(f"dataset file not found: {dataset_path}")

    validation = _validate_dataset(dataset_path)
    if not validation.ok:
        raise BacktestError(
            "dataset validation failed: "
            f"{validation.invalid_rows} invalid row(s) in {validation.source_path}; "
            f"first errors: {list(validation.errors)[:3]}"
        )

    snapshot = load_portfolio_snapshot(config.portfolio_snapshot_path)
    policy = DecisionPolicyService()

    raw_records = load_historical_records(str(dataset_path))
    rows: List[BacktestRowResult] = []
    skipped = 0
    for idx, raw in enumerate(raw_records):
        result = _replay_row(idx, raw, config=config, snapshot=snapshot, policy=policy)
        if result is None:
            skipped += 1
            continue
        rows.append(result)

    aggregates = _aggregate(rows)
    reliability = _reliability_curve(rows)
    domain_break = _domain_breakdown(rows)

    report = BacktestReport(
        schema_version=BACKTEST_SCHEMA_VERSION,
        generated_with={
            "decision_policy_version": DECISION_POLICY_VERSION,
            "uncertainty_model_version": UNCERTAINTY_MODEL_VERSION,
        },
        config=_config_audit(config),
        n_rows=len(rows),
        n_skipped=int(skipped),
        pass_count=int(aggregates["pass_count"]),
        reject_count=int(aggregates["reject_count"]),
        manual_review_count=int(aggregates["manual_review_count"]),
        pass_rate=float(aggregates["pass_rate"]),
        reject_rate=float(aggregates["reject_rate"]),
        manual_review_rate=float(aggregates["manual_review_rate"]),
        realized_positive_rate=float(aggregates["realized_positive_rate"]),
        mean_predicted_probability=float(aggregates["mean_predicted_probability"]),
        mean_predicted_minus_realized=float(aggregates["mean_predicted_minus_realized"]),
        brier_score=float(aggregates["brier_score"]),
        reliability_curve=reliability,
        domain_breakdown=domain_break,
        rows=tuple(rows),
    )

    if config.output_path is not None:
        out = Path(config.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(report.to_canonical_bytes())

    return report


__all__ = [
    "BACKTEST_SCHEMA_VERSION",
    "BacktestConfig",
    "BacktestError",
    "BacktestReport",
    "BacktestRowResult",
    "ReliabilityBin",
    "load_portfolio_snapshot",
    "run_backtest",
]
