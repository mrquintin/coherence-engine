"""Canonical fund-pipeline event schemas and offline validator.

Schemas live alongside this module in ``coherence_engine/server/fund/schemas/events``.
Validation uses ``jsonschema`` Draft 2020-12 when available; otherwise falls back to a
minimal required-keys check and emits a one-time warning.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, List

_LOG = logging.getLogger(__name__)

try:
    from jsonschema import Draft202012Validator  # type: ignore
    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via fallback path
    Draft202012Validator = None  # type: ignore
    _JSONSCHEMA_AVAILABLE = False


SUPPORTED_EVENTS: Dict[str, List[str]] = {
    "interview_completed": ["1"],
    "argument_compiled": ["1"],
    "decision_issued": ["1"],
    "founder_notified": ["1"],
}


_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "events"
_schema_cache: Dict[str, Dict[str, object]] = {}
_fallback_warning_lock = threading.Lock()
_fallback_warning_emitted = False


class EventValidationError(ValueError):
    """Raised when an event payload fails schema validation."""


def _emit_fallback_warning_once() -> None:
    global _fallback_warning_emitted
    with _fallback_warning_lock:
        if _fallback_warning_emitted:
            return
        _fallback_warning_emitted = True
    _LOG.warning(
        "jsonschema not installed; event_schemas is using a minimal required-keys "
        "fallback validator. Install the 'jsonschema' package for full validation."
    )


def _schema_path(event_name: str, version: str) -> Path:
    return _SCHEMA_DIR / f"{event_name}.v{version}.json"


def load_schema(event_name: str, version: str = "1") -> Dict[str, object]:
    """Load and cache the JSON schema document for an event name + version."""
    versions = SUPPORTED_EVENTS.get(event_name)
    if not versions or version not in versions:
        raise EventValidationError(
            f"Unsupported event '{event_name}' version '{version}'"
        )
    cache_key = f"{event_name}:{version}"
    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached
    path = _schema_path(event_name, version)
    if not path.exists():
        raise EventValidationError(f"Schema file missing for {event_name} v{version}: {path}")
    with path.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    _schema_cache[cache_key] = schema
    return schema


def _fallback_validate(schema: Dict[str, object], payload: Dict[str, object]) -> None:
    required = schema.get("required") or []
    if not isinstance(required, list):
        return
    missing = [k for k in required if k not in payload]
    if missing:
        raise EventValidationError(f"Missing required fields: {sorted(missing)}")
    if schema.get("additionalProperties") is False:
        declared = set((schema.get("properties") or {}).keys())
        extras = [k for k in payload.keys() if k not in declared]
        if extras:
            raise EventValidationError(f"Unexpected fields: {sorted(extras)}")


def validate_event(
    event_name: str,
    payload: Dict[str, object],
    version: str = "1",
) -> None:
    """Validate ``payload`` against the schema registered for ``event_name``.

    Raises :class:`EventValidationError` on any failure.
    """
    schema = load_schema(event_name, version)
    if _JSONSCHEMA_AVAILABLE:
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            path = ".".join(str(p) for p in first.absolute_path) or "<root>"
            raise EventValidationError(
                f"{event_name} v{version} payload invalid at {path}: {first.message}"
            )
        return
    _emit_fallback_warning_once()
    _fallback_validate(schema, payload)
