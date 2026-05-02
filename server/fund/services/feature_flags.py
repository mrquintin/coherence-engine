"""Feature-flag resolver with audit invariants for restricted flags.

This module is the single entry point for reading feature flags from
the runtime. Flags are declared in
``data/governed/feature_flags.yaml`` (the registry) and resolved in
this order:

  1. Configured backend (LaunchDarkly / PostHog), if any.
  2. YAML default declared in the registry.
  3. Caller-supplied default passed to the ``get_*`` accessor.
  4. Raise :class:`MissingFlag` (the registry is authoritative —
     unknown keys are bugs, not configuration mistakes).

Restricted flags (``restricted: true`` in the registry) are flags
that change *decision policy* semantics. Any change to a restricted
flag MUST go through :meth:`FeatureFlags.set_restricted`, which:

* writes a JSONL audit row to ``data/governed/feature_flag_audit.log``
  capturing actor, source, old value, new value, and reason;
* publishes a ``decision_policy_flag_changed.v1`` event so backtests
  can replay against the old flag state.

The 60-second result cache exists to keep hot scoring loops cheap.
``flags set`` (and :meth:`FeatureFlags.invalidate_cache`) clear the
cache so an operator flip is visible on the next read.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from coherence_engine.server.fund.services.feature_flag_backends import (
    FeatureFlagBackend,
    NullBackend,
    build_backend,
)

_LOG = logging.getLogger(__name__)

SCHEMA_VERSION = "feature-flags-v1"
FLAG_TYPES = ("boolean", "string-enum", "int-percent")
CACHE_TTL_SECONDS = 60

_DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "governed" / "feature_flags.yaml"
)
_DEFAULT_AUDIT_LOG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "governed" / "feature_flag_audit.log"
)


class FeatureFlagError(RuntimeError):
    """Base class for feature-flag errors."""


class MissingFlag(FeatureFlagError):
    """Raised when a flag key is absent from the registry."""


class RestrictedFlagViolation(FeatureFlagError):
    """Raised when a restricted flag is mutated through a non-audit path."""


class FlagTypeError(FeatureFlagError):
    """Raised when a value cannot be coerced to a flag's declared type."""


@dataclass(frozen=True)
class FlagSpec:
    """Declarative description of a single flag from the registry."""

    key: str
    type: str
    default: Any
    restricted: bool
    client_visible: bool
    owner: str
    description: str
    enum: Tuple[str, ...] = ()

    def coerce(self, raw: Any) -> Any:
        """Coerce ``raw`` to this flag's declared Python type, or raise."""
        if raw is None:
            raise FlagTypeError(f"flag={self.key} value=None")
        if self.type == "boolean":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                low = raw.strip().lower()
                if low in {"true", "1", "yes", "on"}:
                    return True
                if low in {"false", "0", "no", "off"}:
                    return False
            if isinstance(raw, (int,)) and not isinstance(raw, bool):
                return bool(raw)
            raise FlagTypeError(f"flag={self.key} expected_boolean got={raw!r}")
        if self.type == "string-enum":
            if not isinstance(raw, str):
                raise FlagTypeError(f"flag={self.key} expected_string got={raw!r}")
            if self.enum and raw not in self.enum:
                raise FlagTypeError(
                    f"flag={self.key} value={raw!r} not_in_enum={list(self.enum)}"
                )
            return raw
        if self.type == "int-percent":
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise FlagTypeError(f"flag={self.key} expected_int got={raw!r}") from exc
            if value < 0 or value > 100:
                raise FlagTypeError(f"flag={self.key} percent_out_of_range={value}")
            return value
        raise FlagTypeError(f"flag={self.key} unknown_type={self.type}")


def _load_registry(path: Path) -> Dict[str, FlagSpec]:
    if not path.exists():
        raise FeatureFlagError(f"feature_flags_registry_missing:{path}")
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise FeatureFlagError(
            f"feature_flags_registry_schema_mismatch:{doc.get('schema_version')!r}"
        )
    flags = doc.get("flags") or []
    out: Dict[str, FlagSpec] = {}
    for entry in flags:
        if not isinstance(entry, dict):
            raise FeatureFlagError(f"feature_flags_registry_bad_entry:{entry!r}")
        try:
            key = entry["key"]
            ftype = entry["type"]
            default = entry["default"]
        except KeyError as exc:
            raise FeatureFlagError(f"feature_flags_missing_field:{exc}:{entry!r}") from exc
        if ftype not in FLAG_TYPES:
            raise FeatureFlagError(f"feature_flags_unknown_type:{ftype!r}:key={key}")
        enum_raw = entry.get("enum") or ()
        if ftype == "string-enum" and not enum_raw:
            raise FeatureFlagError(f"feature_flags_string_enum_missing_enum:{key}")
        spec = FlagSpec(
            key=key,
            type=ftype,
            default=default,
            restricted=bool(entry.get("restricted", False)),
            client_visible=bool(entry.get("client_visible", False)),
            owner=str(entry.get("owner", "")),
            description=str(entry.get("description", "")),
            enum=tuple(enum_raw),
        )
        # Validate the declared default against the declared type up-front.
        try:
            spec.coerce(spec.default)
        except FlagTypeError as exc:
            raise FeatureFlagError(f"feature_flags_default_invalid:{key}:{exc}") from exc
        if spec.restricted and spec.client_visible:
            raise FeatureFlagError(
                f"feature_flags_restricted_must_not_be_client_visible:{key}"
            )
        out[key] = spec
    return out


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


class FeatureFlags:
    """Process-wide feature-flag resolver."""

    def __init__(
        self,
        *,
        registry_path: Optional[Path] = None,
        backend: Optional[FeatureFlagBackend] = None,
        audit_log_path: Optional[Path] = None,
        event_publisher: Optional[Callable[[Dict[str, Any]], None]] = None,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
        actor: Optional[str] = None,
    ) -> None:
        self._registry_path = registry_path or _DEFAULT_REGISTRY_PATH
        self._registry = _load_registry(self._registry_path)
        self._backend = backend if backend is not None else build_backend()
        self._audit_log_path = audit_log_path or _DEFAULT_AUDIT_LOG_PATH
        self._event_publisher = event_publisher
        self._cache_ttl = max(0, int(cache_ttl_seconds))
        self._cache: Dict[str, Tuple[float, Any, str]] = {}
        self._lock = threading.Lock()
        self._yaml_overrides: Dict[str, Any] = {}
        self._actor = actor or os.getenv("COHERENCE_FUND_FEATURE_FLAGS_ACTOR", "system")

    # -- Introspection -------------------------------------------------

    @property
    def backend_name(self) -> str:
        return getattr(self._backend, "name", "unknown")

    def list_flags(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for key, spec in sorted(self._registry.items()):
            value, source = self._resolve(key, raise_on_missing=True)
            rows.append(
                {
                    "key": key,
                    "type": spec.type,
                    "value": value,
                    "default": spec.default,
                    "source": source,
                    "restricted": spec.restricted,
                    "client_visible": spec.client_visible,
                    "owner": spec.owner,
                    "description": spec.description,
                }
            )
        return rows

    def public_flags(self) -> Dict[str, Any]:
        """Return only flags with ``client_visible: true``.

        Restricted flags are *never* exposed by this API even if a
        registry typo set ``client_visible: true`` on one — the loader
        already rejects that combination at boot, this is defense in
        depth.
        """
        out: Dict[str, Any] = {}
        for key, spec in self._registry.items():
            if not spec.client_visible or spec.restricted:
                continue
            value, _ = self._resolve(key, raise_on_missing=True)
            out[key] = value
        return out

    def spec(self, key: str) -> FlagSpec:
        try:
            return self._registry[key]
        except KeyError as exc:
            raise MissingFlag(f"unknown_flag:{key!r}") from exc

    # -- Read accessors ------------------------------------------------

    def get_bool(self, key: str, default: Optional[bool] = None) -> bool:
        return self._typed_get(key, "boolean", default)

    def get_string(self, key: str, default: Optional[str] = None) -> str:
        return self._typed_get(key, "string-enum", default)

    def get_percent(self, key: str, default: Optional[int] = None) -> int:
        return self._typed_get(key, "int-percent", default)

    def _typed_get(self, key: str, expected_type: str, caller_default: Any) -> Any:
        spec = self.spec(key)
        if spec.type != expected_type:
            raise FlagTypeError(
                f"flag={key} declared_type={spec.type} requested_type={expected_type}"
            )
        try:
            value, _ = self._resolve(key, raise_on_missing=False)
            return value
        except MissingFlag:
            if caller_default is None:
                raise
            return spec.coerce(caller_default)

    def _resolve(self, key: str, *, raise_on_missing: bool) -> Tuple[Any, str]:
        spec = self.spec(key)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            expires, value, source = cached
            if expires > now:
                return value, source
        value, source = self._resolve_uncached(spec)
        if value is None and raise_on_missing:
            raise MissingFlag(f"unresolvable_flag:{key}")
        with self._lock:
            self._cache[key] = (now + self._cache_ttl, value, source)
        return value, source

    def _resolve_uncached(self, spec: FlagSpec) -> Tuple[Any, str]:
        # 1. Backend
        if not isinstance(self._backend, NullBackend):
            raw = self._backend.get(spec.key, spec.type)
            if raw is not None:
                try:
                    return spec.coerce(raw), self._backend.name
                except FlagTypeError:
                    _LOG.warning(
                        "feature_flag_backend_value_invalid key=%s backend=%s",
                        spec.key,
                        self._backend.name,
                    )
        # 2. YAML override (in-memory; flushed by `flags set` in non-prod)
        if spec.key in self._yaml_overrides:
            return spec.coerce(self._yaml_overrides[spec.key]), "yaml"
        # 3. YAML default
        return spec.coerce(spec.default), "yaml"

    # -- Mutation ------------------------------------------------------

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def set_unrestricted(
        self,
        key: str,
        new_value: Any,
        *,
        actor: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a non-restricted flag.

        For local YAML/dev environments only — the prod path runs through
        LaunchDarkly/PostHog's own dashboard and never touches the
        local YAML registry file.
        """
        spec = self.spec(key)
        if spec.restricted:
            raise RestrictedFlagViolation(
                f"flag={key} is restricted; use set_restricted(...)"
            )
        coerced = spec.coerce(new_value)
        old_value, _ = self._resolve_uncached(spec)
        self._yaml_overrides[key] = coerced
        self.invalidate_cache()
        return {
            "key": key,
            "old_value": old_value,
            "new_value": coerced,
            "actor": actor or self._actor,
            "reason": reason or "",
            "audit_id": None,
        }

    def set_restricted(
        self,
        key: str,
        new_value: Any,
        *,
        actor: str,
        reason: str,
        source: str = "cli",
    ) -> Dict[str, Any]:
        """Flip a restricted flag.

        Writes an audit row AND emits a ``decision_policy_flag_changed``
        event. Both are required — a missing event publisher raises so
        the caller cannot silently bypass the audit invariant.
        """
        spec = self.spec(key)
        if not spec.restricted:
            raise RestrictedFlagViolation(
                f"flag={key} is not restricted; use set_unrestricted(...)"
            )
        if not actor:
            raise RestrictedFlagViolation("actor required for restricted flag changes")
        if not reason:
            raise RestrictedFlagViolation("reason required for restricted flag changes")
        coerced = spec.coerce(new_value)
        old_value, _ = self._resolve_uncached(spec)
        audit_id = f"ffaud_{uuid.uuid4().hex[:16]}"
        audit_row = {
            "audit_id": audit_id,
            "occurred_at": _utc_now_iso(),
            "key": key,
            "flag_type": spec.type,
            "restricted": True,
            "old_value": old_value,
            "new_value": coerced,
            "actor": actor,
            "source": source,
            "reason": reason,
        }
        self._write_audit_row(audit_row)
        self._yaml_overrides[key] = coerced
        self.invalidate_cache()
        self._publish_change_event(audit_row)
        return audit_row

    def _write_audit_row(self, row: Dict[str, Any]) -> None:
        path = self._audit_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def _publish_change_event(self, row: Dict[str, Any]) -> None:
        publisher = self._event_publisher
        if publisher is None:
            # Restricted-flag changes MUST emit. If no publisher was wired
            # in, raise so the caller cannot silently bypass the audit.
            raise RestrictedFlagViolation(
                "feature_flags has no event_publisher; restricted-flag change "
                "would be unobservable to backtest replay"
            )
        publisher(row)


_DEFAULT_EVENT_LOG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "governed" / "feature_flag_events.jsonl"
)


def jsonl_event_emitter(
    log_path: Optional[Path] = None,
) -> Callable[[Dict[str, Any]], None]:
    """Return an event emitter that appends events to a JSONL file.

    Used by the CLI (and other non-DB callers) so a restricted-flag flip
    still produces a ``decision_policy_flag_changed.v1`` envelope on
    disk that backtest tooling can consume even when no DB / outbox is
    available.
    """
    target = log_path or _DEFAULT_EVENT_LOG_PATH

    def _emit(row: Dict[str, Any]) -> None:
        envelope = {
            "event_id": str(uuid.uuid4()),
            "event_name": "decision_policy_flag_changed",
            "schema_version": 1,
            "occurred_at": row.get("occurred_at", _utc_now_iso()),
            "key": row["key"],
            "flag_type": row["flag_type"],
            "restricted": True,
            "old_value": row["old_value"],
            "new_value": row["new_value"],
            "actor": row["actor"],
            "source": row.get("source", "cli"),
            "audit_id": row["audit_id"],
        }
        reason = row.get("reason")
        if reason:
            envelope["reason"] = reason
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope, sort_keys=True) + "\n")

    return _emit


# ── Module-level singleton ----------------------------------------------------

_singleton: Optional[FeatureFlags] = None
_singleton_lock = threading.Lock()


def get_feature_flags() -> FeatureFlags:
    """Return the lazily constructed module-level :class:`FeatureFlags`.

    Tests should not rely on this — they should construct their own
    :class:`FeatureFlags` instance with an explicit registry path and
    a stub publisher.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = FeatureFlags(event_publisher=jsonl_event_emitter())
    return _singleton


def reset_singleton_for_tests() -> None:
    """Clear the module-level singleton. Test-only."""
    global _singleton
    with _singleton_lock:
        _singleton = None
