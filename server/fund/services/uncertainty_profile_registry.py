"""Local JSON registry for uncertainty calibration profiles with staged promotion and rollback.

Objective gates, signed audit JSONL, and rollback trigger helpers live in
``uncertainty_governance`` (used by the ``uncertainty-profile`` CLI).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple

REGISTRY_SCHEMA_VERSION = 1

STAGES = ("shadow", "canary", "prod")


class RegistryError(ValueError):
    """Invalid registry operation or corrupt file."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_registry() -> Dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "stages": {
            s: {"active": None, "rollback_stack": []} for s in STAGES
        },
        "history": [],
    }


def _ensure_stage(stage: str) -> str:
    if stage not in STAGES:
        raise RegistryError(f"stage must be one of {list(STAGES)}, got {stage!r}")
    return stage


def load_registry(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return empty_registry()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read registry: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryError("registry root must be a JSON object")
    if raw.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("unsupported registry schema_version")
    stages = raw.get("stages")
    if not isinstance(stages, dict):
        raise RegistryError("registry.stages missing or not an object")
    for s in STAGES:
        block = stages.get(s)
        if not isinstance(block, dict):
            raise RegistryError(f"registry.stages.{s} must be an object")
        if "active" not in block or "rollback_stack" not in block:
            raise RegistryError(f"registry.stages.{s} must have active and rollback_stack")
        rs = block["rollback_stack"]
        if not isinstance(rs, list):
            raise RegistryError(f"registry.stages.{s}.rollback_stack must be a list")
    hist = raw.get("history")
    if hist is not None and not isinstance(hist, list):
        raise RegistryError("registry.history must be a list when present")
    return raw


def save_registry(path: str | Path, data: Mapping[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent),
        prefix=".uncertainty_registry_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_profile_json(profile_path: str | Path) -> Dict[str, Any]:
    p = Path(profile_path)
    if not p.is_file():
        raise RegistryError(f"profile file not found: {p}")
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid profile JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RegistryError("profile root must be a JSON object")
    return dict(obj)


def verify_manifest_checksum(
    dataset_path: str | Path,
    manifest_path: str | Path,
    *,
    algorithm: str = "sha256",
) -> str:
    """
    Verify dataset bytes match manifest checksum.

    Returns the computed digest (lowercase hex) on success.
    Raises RegistryError on mismatch or invalid manifest.
    """
    dpath = Path(dataset_path)
    mpath = Path(manifest_path)
    if not dpath.is_file():
        raise RegistryError(f"dataset not found: {dpath}")
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"invalid manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RegistryError("manifest root must be a JSON object")
    expected = manifest.get("checksum_sha256") or manifest.get("checksum")
    if not expected or not isinstance(expected, str):
        raise RegistryError("manifest must contain checksum_sha256 (or checksum) string")
    algo = manifest.get("algorithm", algorithm)
    if algo != "sha256":
        raise RegistryError(f"unsupported manifest algorithm: {algo!r}")
    body = dpath.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest.lower() != expected.lower():
        raise RegistryError(
            f"checksum mismatch for {dpath.name}: expected {expected}, got {digest}"
        )
    return digest


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of a promote or rollback (for CLI / callers)."""

    stage: str
    action: str
    registry: Dict[str, Any]


def _append_history(
    reg: MutableMapping[str, Any],
    event: str,
    stage: str,
    detail: Mapping[str, Any],
) -> None:
    hist = reg.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        reg["history"] = hist
    entry = {
        "event": event,
        "stage": stage,
        "at": _utc_now_iso(),
        **detail,
    }
    hist.append(entry)


def promote(
    registry_path: str | Path,
    stage: str,
    profile_path: str | Path,
    *,
    reason: str = "",
    recorded_at: Optional[str] = None,
) -> PromotionResult:
    """
    Set stage's active profile from JSON file.

    Previous active profile (if any) is pushed onto that stage's rollback_stack
    (LIFO). Deterministic given registry state and profile file contents.
    """
    _ensure_stage(stage)
    reg = load_registry(registry_path)
    profile = read_profile_json(profile_path)
    ts = recorded_at or _utc_now_iso()
    stages = reg["stages"]
    block = stages[stage]
    prev = block.get("active")
    stack: List[Any] = list(block.get("rollback_stack") or [])
    if prev is not None:
        stack.append(prev)
    block["active"] = {
        "profile": profile,
        "recorded_at": ts,
        "source_path": str(Path(profile_path).resolve()),
        "reason": reason or None,
    }
    block["rollback_stack"] = stack
    _append_history(
        reg,
        "promote",
        stage,
        {
            "source_path": str(Path(profile_path).resolve()),
            "reason": reason or None,
            "had_previous": prev is not None,
        },
    )
    save_registry(registry_path, reg)
    return PromotionResult(stage=stage, action="promote", registry=reg)


def rollback(registry_path: str | Path, stage: str) -> PromotionResult:
    """
    Restore the previous active profile for this stage.

    Pops the tip of rollback_stack and makes it active. The current active
    profile is discarded. Fails if rollback_stack is empty (deterministic:
    no prior version to restore).
    """
    _ensure_stage(stage)
    reg = load_registry(registry_path)
    block = reg["stages"][stage]
    stack: List[Any] = list(block.get("rollback_stack") or [])
    if not stack:
        raise RegistryError(
            f"cannot rollback stage {stage!r}: rollback_stack is empty"
        )
    previous = stack.pop()
    block["rollback_stack"] = stack
    replaced = block.get("active")
    block["active"] = previous
    _append_history(
        reg,
        "rollback",
        stage,
        {
            "discarded_recorded_at": replaced.get("recorded_at") if isinstance(replaced, dict) else None,
            "restored_recorded_at": previous.get("recorded_at") if isinstance(previous, dict) else None,
        },
    )
    save_registry(registry_path, reg)
    return PromotionResult(stage=stage, action="rollback", registry=reg)


def get_stage_view(registry_path: str | Path, stage: str) -> Tuple[Optional[Dict[str, Any]], int]:
    """Return (active_entry_or_none, rollback_depth)."""
    _ensure_stage(stage)
    reg = load_registry(registry_path)
    block = reg["stages"][stage]
    active = block.get("active")
    if active is not None and not isinstance(active, dict):
        raise RegistryError("corrupt active entry")
    stack = block.get("rollback_stack") or []
    return (active if active is not None else None), len(stack)


def export_runtime_profile_dict(active_entry: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Flatten calibration output for COHERENCE_UNCERTAINTY_PROFILE-style files.

    Uses best_parameters if present; otherwise treats profile as already flat tunables.
    """
    prof = active_entry.get("profile")
    if not isinstance(prof, dict):
        raise RegistryError("active entry has no profile object")
    inner = prof.get("best_parameters")
    if isinstance(inner, dict):
        return dict(inner)
    keys = {
        "sigma0",
        "z95",
        "alpha_quality",
        "alpha_burden",
        "alpha_disagreement",
        "half_min",
        "half_max",
    }
    if keys.intersection(prof.keys()):
        return {k: prof[k] for k in keys if k in prof}
    raise RegistryError("profile has neither best_parameters nor tunable keys")
