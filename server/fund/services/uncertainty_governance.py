"""Objective quality gates, signed audit records, and rollback trigger policy for calibration profiles."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

GOVERNANCE_HMAC_ENV = "COHERENCE_UNCERTAINTY_GOVERNANCE_HMAC_KEY"

GOVERNANCE_POLICY_STAGES = frozenset({"shadow", "canary", "prod"})

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "data" / "governed" / "uncertainty_governance_policy.json"


class GovernanceError(ValueError):
    """Invalid governance input or policy configuration."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extract_calibration_metrics(calibration: Mapping[str, Any]) -> Dict[str, float]:
    """
    Pull objective metrics from calibrate-uncertainty / run_calibration_pipeline JSON.

    Returns coverage, mean_width, record_count (float for uniform typing in JSON).
    """
    if not isinstance(calibration, Mapping):
        raise GovernanceError("calibration must be a mapping")
    metrics = calibration.get("metrics")
    if not isinstance(metrics, Mapping):
        raise GovernanceError("calibration.metrics missing or not an object")
    cov = metrics.get("coverage")
    mw = metrics.get("mean_width")
    if cov is None or mw is None:
        raise GovernanceError("calibration.metrics must include coverage and mean_width")
    n_used = calibration.get("n_records_used")
    n_rec = calibration.get("n_records")
    n_eval = metrics.get("n_evaluated")
    if n_used is not None:
        rc = int(n_used)
    elif n_rec is not None:
        rc = int(n_rec)
    elif n_eval is not None:
        rc = int(n_eval)
    else:
        raise GovernanceError(
            "calibration must include n_records_used, n_records, or metrics.n_evaluated"
        )
    return {
        "coverage": float(cov),
        "mean_width": float(mw),
        "record_count": float(rc),
    }


@dataclass(frozen=True)
class GateThresholds:
    """Objective gates for approving a candidate calibration profile."""

    min_coverage: Optional[float] = None
    max_mean_width: Optional[float] = None
    min_record_count: Optional[int] = None
    max_coverage_drop_vs_baseline: Optional[float] = None
    max_mean_width_increase_vs_baseline: Optional[float] = None


def gate_thresholds_any_set(thresholds: GateThresholds) -> bool:
    """True if at least one promotion gate threshold is configured."""
    return any(
        getattr(thresholds, name) is not None
        for name in (
            "min_coverage",
            "max_mean_width",
            "min_record_count",
            "max_coverage_drop_vs_baseline",
            "max_mean_width_increase_vs_baseline",
        )
    )


def _optional_float(raw: Mapping[str, Any], key: str) -> Optional[float]:
    if key not in raw or raw[key] is None:
        return None
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise GovernanceError(f"promotion_gates.{key} must be a number or null")
    return float(v)


def _optional_int(raw: Mapping[str, Any], key: str) -> Optional[int]:
    if key not in raw or raw[key] is None:
        return None
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise GovernanceError(f"promotion_gates.{key} must be an integer or null")
    return int(v)


def gate_thresholds_from_mapping(raw: Mapping[str, Any]) -> GateThresholds:
    """Build GateThresholds from a JSON object (null/absent keys ignored)."""
    if not isinstance(raw, Mapping):
        raise GovernanceError("promotion_gates must be a JSON object")
    return GateThresholds(
        min_coverage=_optional_float(raw, "min_coverage"),
        max_mean_width=_optional_float(raw, "max_mean_width"),
        min_record_count=_optional_int(raw, "min_record_count"),
        max_coverage_drop_vs_baseline=_optional_float(raw, "max_coverage_drop_vs_baseline"),
        max_mean_width_increase_vs_baseline=_optional_float(
            raw, "max_mean_width_increase_vs_baseline"
        ),
    )


@dataclass(frozen=True)
class UncertaintyGovernancePolicy:
    """Stage-keyed promotion gates and optional rollback triggers (file-backed)."""

    schema_version: int
    stages: Mapping[str, Mapping[str, Any]]
    source_path: Optional[str] = None

    def promotion_gate_thresholds(self, stage: str) -> GateThresholds:
        if stage not in GOVERNANCE_POLICY_STAGES:
            raise GovernanceError(f"invalid stage {stage!r}; expected one of {sorted(GOVERNANCE_POLICY_STAGES)}")
        block = self.stages.get(stage)
        if not isinstance(block, Mapping):
            raise GovernanceError(f"policy.stages.{stage} missing or not an object")
        pg = block.get("promotion_gates")
        if pg is None:
            return GateThresholds()
        if not isinstance(pg, Mapping):
            raise GovernanceError(f"policy.stages.{stage}.promotion_gates must be an object")
        return gate_thresholds_from_mapping(pg)

    def rollback_triggers(self, stage: str) -> Optional[RollbackPolicy]:
        if stage not in GOVERNANCE_POLICY_STAGES:
            raise GovernanceError(f"invalid stage {stage!r}; expected one of {sorted(GOVERNANCE_POLICY_STAGES)}")
        block = self.stages.get(stage)
        if not isinstance(block, Mapping):
            return None
        rt = block.get("rollback_triggers")
        if rt is None:
            return None
        return rollback_policy_from_mapping(rt)


def load_uncertainty_governance_policy(path: str | Path | None = None) -> UncertaintyGovernancePolicy:
    """
    Load JSON policy with per-stage promotion_gates (and optional rollback_triggers).

    Deterministic: reads only the given path (default: repo data/governed file). No network.
    """
    p = Path(path) if path is not None else _DEFAULT_POLICY_PATH
    if not p.is_file():
        raise GovernanceError(f"governance policy file not found: {p}")
    try:
        root = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"invalid governance policy JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise GovernanceError("governance policy root must be a JSON object")
    ver = root.get("schema_version")
    if ver != 1:
        raise GovernanceError("governance policy schema_version must be 1")
    stages = root.get("stages")
    if not isinstance(stages, dict):
        raise GovernanceError("governance policy stages must be an object")
    for name in GOVERNANCE_POLICY_STAGES:
        if name not in stages:
            raise GovernanceError(f"governance policy missing stages.{name}")
        if not isinstance(stages[name], dict):
            raise GovernanceError(f"governance policy stages.{name} must be an object")
    return UncertaintyGovernancePolicy(
        schema_version=int(ver),
        stages=stages,
        source_path=str(p.resolve()),
    )


def merge_gate_thresholds(
    base: GateThresholds,
    override: GateThresholds,
) -> GateThresholds:
    """Non-None fields in override replace base (CLI overrides policy)."""

    def pick(
        b: Optional[Any],
        o: Optional[Any],
    ) -> Optional[Any]:
        return o if o is not None else b

    return GateThresholds(
        min_coverage=pick(base.min_coverage, override.min_coverage),
        max_mean_width=pick(base.max_mean_width, override.max_mean_width),
        min_record_count=pick(base.min_record_count, override.min_record_count),
        max_coverage_drop_vs_baseline=pick(
            base.max_coverage_drop_vs_baseline,
            override.max_coverage_drop_vs_baseline,
        ),
        max_mean_width_increase_vs_baseline=pick(
            base.max_mean_width_increase_vs_baseline,
            override.max_mean_width_increase_vs_baseline,
        ),
    )


@dataclass(frozen=True)
class GateEvaluation:
    approved: bool
    metrics: Dict[str, float]
    failures: Tuple[str, ...] = ()
    baseline_metrics: Optional[Dict[str, float]] = None


def evaluate_quality_gates(
    calibration: Mapping[str, Any],
    thresholds: GateThresholds,
    *,
    baseline_calibration: Optional[Mapping[str, Any]] = None,
) -> GateEvaluation:
    """
    Approve or reject candidate profile using objective thresholds and optional deltas vs baseline.
    """
    m = extract_calibration_metrics(calibration)
    failures: List[str] = []
    base_m: Optional[Dict[str, float]] = None

    if thresholds.min_coverage is not None and m["coverage"] < thresholds.min_coverage:
        failures.append(
            f"coverage {m['coverage']:.6f} < min_coverage {thresholds.min_coverage:.6f}"
        )
    if thresholds.max_mean_width is not None and m["mean_width"] > thresholds.max_mean_width:
        failures.append(
            f"mean_width {m['mean_width']:.6f} > max_mean_width {thresholds.max_mean_width:.6f}"
        )
    if thresholds.min_record_count is not None and int(m["record_count"]) < thresholds.min_record_count:
        failures.append(
            f"record_count {int(m['record_count'])} < min_record_count {thresholds.min_record_count}"
        )

    if baseline_calibration is not None:
        base_m = extract_calibration_metrics(baseline_calibration)
        if thresholds.max_coverage_drop_vs_baseline is not None:
            drop = base_m["coverage"] - m["coverage"]
            if drop > thresholds.max_coverage_drop_vs_baseline:
                failures.append(
                    f"coverage_drop {drop:.6f} > max_coverage_drop_vs_baseline "
                    f"{thresholds.max_coverage_drop_vs_baseline:.6f}"
                )
        if thresholds.max_mean_width_increase_vs_baseline is not None:
            inc = m["mean_width"] - base_m["mean_width"]
            if inc > thresholds.max_mean_width_increase_vs_baseline:
                failures.append(
                    f"mean_width_increase {inc:.6f} > max_mean_width_increase_vs_baseline "
                    f"{thresholds.max_mean_width_increase_vs_baseline:.6f}"
                )

    return GateEvaluation(
        approved=len(failures) == 0,
        metrics=m,
        failures=tuple(failures),
        baseline_metrics=base_m,
    )


_SIGN_EXCLUDE_KEYS = frozenset({"signature"})


def canonical_signing_bytes(record: Mapping[str, Any]) -> bytes:
    """Deterministic UTF-8 JSON for HMAC input (excludes signature fields)."""
    payload = {k: record[k] for k in sorted(record.keys()) if k not in _SIGN_EXCLUDE_KEYS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def governance_hmac_key_bytes() -> Optional[bytes]:
    raw = os.environ.get(GOVERNANCE_HMAC_ENV)
    if raw is None or raw == "":
        return None
    return raw.encode("utf-8")


def sign_audit_record(record: MutableMapping[str, Any]) -> Dict[str, Any]:
    """
    Add HMAC-SHA256 signature when GOVERNANCE_HMAC_ENV is set; otherwise explicit unsigned mode.

    Safe without a key: signing_mode is 'unsigned_no_secret', signature is null.
    """
    key = governance_hmac_key_bytes()
    out = dict(record)
    if key is not None:
        out["signing_mode"] = "hmac_sha256"
        out["signature_algorithm"] = "HMAC-SHA256"
        msg = canonical_signing_bytes(out)
        out["signature"] = hmac.new(key, msg, hashlib.sha256).hexdigest()
    else:
        out["signing_mode"] = "unsigned_no_secret"
        out["signature_algorithm"] = None
        out["signature"] = None
    return out


def verify_audit_record(record: Mapping[str, Any]) -> bool:
    """Return True if record is unsigned_no_secret or HMAC verifies with current env key."""
    mode = record.get("signing_mode")
    if mode == "unsigned_no_secret":
        return record.get("signature") in (None, "")
    if mode != "hmac_sha256":
        return False
    key = governance_hmac_key_bytes()
    if key is None:
        return False
    sig = record.get("signature")
    if not isinstance(sig, str) or not sig:
        return False
    msg = canonical_signing_bytes(record)
    expected = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), sig.lower())


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def append_audit_jsonl(audit_path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one JSON object per line; atomic write of the new line."""
    p = Path(audit_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent),
        prefix=".uncertainty_governance_audit_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if p.is_file():
                fh.write(p.read_text(encoding="utf-8"))
            fh.write(line)
        os.replace(tmp, p)
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


@dataclass(frozen=True)
class RollbackPolicy:
    """
    Local deterministic rollback triggers: any violated threshold recommends rollback.

    All optional fields that are None are ignored.
    """

    min_coverage: Optional[float] = None
    max_mean_width: Optional[float] = None
    min_record_count: Optional[int] = None


def rollback_policy_from_mapping(raw: Mapping[str, Any]) -> RollbackPolicy:
    """Build RollbackPolicy from a JSON object under stages.*.rollback_triggers."""
    if not isinstance(raw, Mapping):
        raise GovernanceError("rollback_triggers must be a JSON object")

    def f(key: str) -> Optional[float]:
        if key not in raw or raw[key] is None:
            return None
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise GovernanceError(f"rollback_triggers.{key} must be a number or null")
        return float(v)

    def i(key: str) -> Optional[int]:
        if key not in raw or raw[key] is None:
            return None
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise GovernanceError(f"rollback_triggers.{key} must be an integer or null")
        return int(v)

    return RollbackPolicy(
        min_coverage=f("min_coverage"),
        max_mean_width=f("max_mean_width"),
        min_record_count=i("min_record_count"),
    )


@dataclass(frozen=True)
class RollbackDecision:
    should_rollback: bool
    reasons: Tuple[str, ...] = field(default_factory=tuple)


def load_metrics_json(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise GovernanceError(f"metrics file not found: {p}")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"invalid metrics JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise GovernanceError("metrics root must be a JSON object")
    return dict(obj)


def normalize_health_metrics(raw: Mapping[str, Any]) -> Dict[str, float]:
    """
    Map flexible JSON keys to coverage, mean_width, record_count for policy checks.

    Accepts flat calibration-style or nested metrics object.
    """
    if "metrics" in raw and isinstance(raw["metrics"], Mapping):
        inner = raw["metrics"]
        cov = inner.get("coverage")
        mw = inner.get("mean_width")
    else:
        cov = raw.get("coverage")
        mw = raw.get("mean_width")
    rc = (
        raw.get("record_count")
        or raw.get("n_records_used")
        or raw.get("n_records")
        or raw.get("n_evaluated")
    )
    if cov is None or mw is None:
        raise GovernanceError("metrics must include coverage and mean_width (top-level or under metrics)")
    if rc is None:
        raise GovernanceError("metrics must include record_count, n_records_used, n_records, or n_evaluated")
    return {
        "coverage": float(cov),
        "mean_width": float(mw),
        "record_count": float(int(rc)),
    }


def evaluate_rollback_trigger(
    health_metrics: Mapping[str, Any],
    policy: RollbackPolicy,
) -> RollbackDecision:
    """Return whether objective health violates policy thresholds (deterministic, no I/O)."""
    m = normalize_health_metrics(health_metrics)
    reasons: List[str] = []
    if policy.min_coverage is not None and m["coverage"] < policy.min_coverage:
        reasons.append(
            f"coverage {m['coverage']:.6f} < policy.min_coverage {policy.min_coverage:.6f}"
        )
    if policy.max_mean_width is not None and m["mean_width"] > policy.max_mean_width:
        reasons.append(
            f"mean_width {m['mean_width']:.6f} > policy.max_mean_width {policy.max_mean_width:.6f}"
        )
    if policy.min_record_count is not None and int(m["record_count"]) < policy.min_record_count:
        reasons.append(
            f"record_count {int(m['record_count'])} < policy.min_record_count {policy.min_record_count}"
        )
    return RollbackDecision(should_rollback=len(reasons) > 0, reasons=tuple(reasons))


def merge_rollback_policy(base: RollbackPolicy, override: RollbackPolicy) -> RollbackPolicy:
    """Non-None fields in override replace base (CLI overrides policy)."""

    def pick(b: Optional[Any], o: Optional[Any]) -> Optional[Any]:
        return o if o is not None else b

    return RollbackPolicy(
        min_coverage=pick(base.min_coverage, override.min_coverage),
        max_mean_width=pick(base.max_mean_width, override.max_mean_width),
        min_record_count=pick(base.min_record_count, override.min_record_count),
    )


def rollback_policy_any_set(policy: RollbackPolicy) -> bool:
    return any(
        getattr(policy, name) is not None
        for name in ("min_coverage", "max_mean_width", "min_record_count")
    )


def build_promotion_audit_record(
    *,
    operation: str,
    stage: str,
    registry_path: str,
    profile_path: str,
    profile_sha256: str,
    gate_evaluation: Optional[GateEvaluation],
    forced: bool,
    reason: str,
    recorded_at: Optional[str] = None,
    governance_policy_path: Optional[str] = None,
    governance_policy_schema_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Assemble audit payload before signing (no signature fields yet)."""
    ge = gate_evaluation
    base: Dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        "stage": stage,
        "at": recorded_at or _utc_now_iso(),
        "registry_path": str(Path(registry_path).resolve()),
        "profile_path": str(Path(profile_path).resolve()),
        "profile_sha256": profile_sha256,
        "forced": forced,
        "reason": reason or None,
        "gate_approved": ge.approved if ge is not None else True,
        "gate_failures": list(ge.failures) if ge is not None else [],
        "metrics": ge.metrics if ge is not None else {},
        "baseline_metrics": ge.baseline_metrics if ge is not None else None,
    }
    if governance_policy_path is not None:
        base["governance_policy_path"] = str(Path(governance_policy_path).resolve())
    if governance_policy_schema_version is not None:
        base["governance_policy_schema_version"] = int(governance_policy_schema_version)
    return sign_audit_record(base)


def build_rollback_audit_record(
    *,
    stage: str,
    registry_path: str,
    reason: str = "",
    policy_decision: Optional[RollbackDecision] = None,
    recorded_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Signed audit line for registry rollback (optional linkage to rollback policy evaluation)."""
    base: Dict[str, Any] = {
        "schema_version": 1,
        "operation": "rollback",
        "stage": stage,
        "at": recorded_at or _utc_now_iso(),
        "registry_path": str(Path(registry_path).resolve()),
        "reason": reason or None,
        "rollback_trigger_recommended": policy_decision.should_rollback if policy_decision else None,
        "rollback_trigger_reasons": list(policy_decision.reasons) if policy_decision else [],
    }
    return sign_audit_record(base)
