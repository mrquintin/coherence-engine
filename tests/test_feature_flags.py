"""Tests for the FeatureFlags service.

Covers registry parsing, type coercion, resolution order across
backends/YAML/caller-default/MissingFlag, the 60s cache, the
restricted-flag audit invariant, and the public-flags filter that
backs ``/api/v1/flags/public``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from coherence_engine.server.fund.services.feature_flags import (
    FeatureFlagError,
    FeatureFlags,
    FlagSpec,
    FlagTypeError,
    MissingFlag,
    RestrictedFlagViolation,
    jsonl_event_emitter,
)
from coherence_engine.server.fund.services.feature_flag_backends import (
    FeatureFlagBackendError,
    NullBackend,
    build_backend,
)


# ── helpers --------------------------------------------------------------


class _StubBackend:
    """Test backend that returns canned values per key."""

    name = "stub"

    def __init__(self, values: Optional[Dict[str, Any]] = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str, flag_type: str) -> Optional[object]:
        return self._values.get(key)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def __call__(self, row: Dict[str, Any]) -> None:
        self.events.append(dict(row))


def _write_registry(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


_VALID_REGISTRY = """\
schema_version: feature-flags-v1
flags:
  - key: anti_gaming.enabled
    type: boolean
    default: true
    restricted: true
    client_visible: false
    owner: platform
    description: master gate
  - key: ui.beta
    type: boolean
    default: false
    restricted: false
    client_visible: true
    owner: frontend
    description: beta UI
  - key: replay.engine
    type: string-enum
    default: deterministic
    enum: [deterministic, probabilistic]
    restricted: false
    client_visible: false
    owner: platform
    description: replay engine selector
  - key: scoring.uncertainty_floor
    type: int-percent
    default: 5
    restricted: true
    client_visible: false
    owner: platform
    description: lower bound on uncertainty
"""


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    p = tmp_path / "feature_flags.yaml"
    _write_registry(p, _VALID_REGISTRY)
    return p


@pytest.fixture
def audit_log_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


@pytest.fixture
def publisher() -> _RecordingPublisher:
    return _RecordingPublisher()


@pytest.fixture
def flags(registry_path: Path, audit_log_path: Path, publisher: _RecordingPublisher) -> FeatureFlags:
    return FeatureFlags(
        registry_path=registry_path,
        backend=NullBackend(),
        audit_log_path=audit_log_path,
        event_publisher=publisher,
    )


# ── registry parsing ---------------------------------------------------------


def test_registry_parses_all_flag_types(flags: FeatureFlags) -> None:
    assert isinstance(flags.spec("anti_gaming.enabled"), FlagSpec)
    assert flags.spec("ui.beta").client_visible is True
    assert flags.spec("replay.engine").enum == ("deterministic", "probabilistic")
    assert flags.spec("scoring.uncertainty_floor").type == "int-percent"


def test_registry_rejects_wrong_schema_version(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    _write_registry(p, "schema_version: feature-flags-v9\nflags: []\n")
    with pytest.raises(FeatureFlagError):
        FeatureFlags(registry_path=p, backend=NullBackend())


def test_registry_rejects_string_enum_without_enum(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    _write_registry(
        p,
        "schema_version: feature-flags-v1\nflags:\n"
        "  - {key: x, type: string-enum, default: a, owner: t, description: d}\n",
    )
    with pytest.raises(FeatureFlagError):
        FeatureFlags(registry_path=p, backend=NullBackend())


def test_registry_rejects_restricted_client_visible_combo(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    _write_registry(
        p,
        "schema_version: feature-flags-v1\nflags:\n"
        "  - {key: x, type: boolean, default: true, restricted: true, "
        "client_visible: true, owner: t, description: d}\n",
    )
    with pytest.raises(FeatureFlagError):
        FeatureFlags(registry_path=p, backend=NullBackend())


def test_registry_rejects_invalid_default(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    _write_registry(
        p,
        "schema_version: feature-flags-v1\nflags:\n"
        "  - {key: x, type: int-percent, default: 250, owner: t, description: d}\n",
    )
    with pytest.raises(FeatureFlagError):
        FeatureFlags(registry_path=p, backend=NullBackend())


# ── resolution order + type coercion -----------------------------------------


def test_get_bool_returns_yaml_default_when_no_backend(flags: FeatureFlags) -> None:
    assert flags.get_bool("anti_gaming.enabled") is True
    assert flags.get_bool("ui.beta") is False


def test_get_string_uses_yaml_default(flags: FeatureFlags) -> None:
    assert flags.get_string("replay.engine") == "deterministic"


def test_get_percent_uses_yaml_default(flags: FeatureFlags) -> None:
    assert flags.get_percent("scoring.uncertainty_floor") == 5


def test_backend_value_overrides_yaml(
    registry_path: Path, audit_log_path: Path, publisher: _RecordingPublisher
) -> None:
    backend = _StubBackend({"ui.beta": True, "replay.engine": "probabilistic"})
    ff = FeatureFlags(
        registry_path=registry_path,
        backend=backend,
        audit_log_path=audit_log_path,
        event_publisher=publisher,
    )
    assert ff.get_bool("ui.beta") is True
    assert ff.get_string("replay.engine") == "probabilistic"


def test_backend_string_value_coerced_to_bool(
    registry_path: Path, audit_log_path: Path, publisher: _RecordingPublisher
) -> None:
    backend = _StubBackend({"ui.beta": "true"})
    ff = FeatureFlags(
        registry_path=registry_path,
        backend=backend,
        audit_log_path=audit_log_path,
        event_publisher=publisher,
    )
    assert ff.get_bool("ui.beta") is True


def test_get_string_rejects_value_outside_enum(
    registry_path: Path, audit_log_path: Path, publisher: _RecordingPublisher
) -> None:
    backend = _StubBackend({"replay.engine": "quantum"})
    ff = FeatureFlags(
        registry_path=registry_path,
        backend=backend,
        audit_log_path=audit_log_path,
        event_publisher=publisher,
    )
    # Backend value falls through (logged + ignored), YAML default wins.
    assert ff.get_string("replay.engine") == "deterministic"


def test_int_percent_rejects_out_of_range(
    registry_path: Path, audit_log_path: Path, publisher: _RecordingPublisher
) -> None:
    spec = FeatureFlags(
        registry_path=registry_path,
        backend=NullBackend(),
        audit_log_path=audit_log_path,
        event_publisher=publisher,
    ).spec("scoring.uncertainty_floor")
    with pytest.raises(FlagTypeError):
        spec.coerce(150)


def test_unknown_flag_raises_missing(flags: FeatureFlags) -> None:
    with pytest.raises(MissingFlag):
        flags.get_bool("does.not.exist")


def test_typed_get_wrong_kind_raises(flags: FeatureFlags) -> None:
    with pytest.raises(FlagTypeError):
        flags.get_bool("replay.engine")  # declared string-enum


def test_caller_default_used_when_yaml_missing(tmp_path: Path) -> None:
    # Build a registry with a bool whose default is None-equivalent: not allowed,
    # so test caller-default path via a bogus flag layered on a stub backend.
    p = tmp_path / "r.yaml"
    _write_registry(
        p,
        "schema_version: feature-flags-v1\nflags:\n"
        "  - {key: t, type: boolean, default: false, owner: t, description: d}\n",
    )
    ff = FeatureFlags(registry_path=p, backend=NullBackend())
    # caller-default is only consulted when resolution returns None; YAML
    # default always succeeds for valid registries, so verify the type:
    assert ff.get_bool("t", default=True) is False


# ── cache behavior -----------------------------------------------------------


def test_cache_serves_stable_value_until_invalidation(
    registry_path: Path, audit_log_path: Path, publisher: _RecordingPublisher
) -> None:
    backend = _StubBackend({"ui.beta": True})
    ff = FeatureFlags(
        registry_path=registry_path,
        backend=backend,
        audit_log_path=audit_log_path,
        event_publisher=publisher,
        cache_ttl_seconds=300,
    )
    assert ff.get_bool("ui.beta") is True
    backend._values["ui.beta"] = False  # type: ignore[attr-defined]
    assert ff.get_bool("ui.beta") is True  # served from cache
    ff.invalidate_cache()
    assert ff.get_bool("ui.beta") is False


# ── restricted-flag audit invariant ------------------------------------------


def test_set_restricted_writes_audit_row_and_emits_event(
    flags: FeatureFlags, audit_log_path: Path, publisher: _RecordingPublisher
) -> None:
    row = flags.set_restricted(
        "anti_gaming.enabled",
        False,
        actor="ops@coherence",
        reason="incident_204",
        source="cli",
    )
    assert row["key"] == "anti_gaming.enabled"
    assert row["old_value"] is True
    assert row["new_value"] is False
    assert row["audit_id"].startswith("ffaud_")

    # Audit JSONL row.
    assert audit_log_path.exists()
    persisted = json.loads(audit_log_path.read_text(encoding="utf-8").strip())
    assert persisted["actor"] == "ops@coherence"
    assert persisted["reason"] == "incident_204"
    assert persisted["restricted"] is True

    # Change event emitted.
    assert len(publisher.events) == 1
    evt = publisher.events[0]
    assert evt["key"] == "anti_gaming.enabled"
    assert evt["actor"] == "ops@coherence"

    # New value visible after cache invalidation.
    assert flags.get_bool("anti_gaming.enabled") is False


def test_set_restricted_requires_actor_and_reason(flags: FeatureFlags) -> None:
    with pytest.raises(RestrictedFlagViolation):
        flags.set_restricted("anti_gaming.enabled", False, actor="", reason="x")
    with pytest.raises(RestrictedFlagViolation):
        flags.set_restricted("anti_gaming.enabled", False, actor="op", reason="")


def test_set_restricted_rejects_non_restricted_flag(flags: FeatureFlags) -> None:
    with pytest.raises(RestrictedFlagViolation):
        flags.set_restricted("ui.beta", True, actor="op", reason="why")


def test_set_unrestricted_rejects_restricted_flag(flags: FeatureFlags) -> None:
    with pytest.raises(RestrictedFlagViolation):
        flags.set_unrestricted("anti_gaming.enabled", False)


def test_restricted_flip_without_publisher_raises(
    registry_path: Path, audit_log_path: Path
) -> None:
    ff = FeatureFlags(
        registry_path=registry_path,
        backend=NullBackend(),
        audit_log_path=audit_log_path,
        event_publisher=None,
    )
    with pytest.raises(RestrictedFlagViolation):
        ff.set_restricted("anti_gaming.enabled", False, actor="op", reason="r")


# ── public/visible filter ----------------------------------------------------


def test_public_flags_excludes_restricted(flags: FeatureFlags) -> None:
    public = flags.public_flags()
    assert "ui.beta" in public
    assert "anti_gaming.enabled" not in public
    assert "scoring.uncertainty_floor" not in public


# ── backend factory ----------------------------------------------------------


def test_build_backend_defaults_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COHERENCE_FUND_FEATURE_FLAGS_BACKEND", raising=False)
    backend = build_backend()
    assert isinstance(backend, NullBackend)


def test_build_backend_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERENCE_FUND_FEATURE_FLAGS_BACKEND", "nonsense")
    with pytest.raises(FeatureFlagBackendError):
        build_backend()


def test_build_launchdarkly_without_sdk_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERENCE_FUND_FEATURE_FLAGS_BACKEND", "launchdarkly")
    monkeypatch.delenv("LAUNCHDARKLY_SDK_KEY", raising=False)
    with pytest.raises(FeatureFlagBackendError):
        build_backend()


def test_build_posthog_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COHERENCE_FUND_FEATURE_FLAGS_BACKEND", "posthog")
    monkeypatch.delenv("POSTHOG_PROJECT_API_KEY", raising=False)
    with pytest.raises(FeatureFlagBackendError):
        build_backend()


# ── jsonl emitter (CLI fallback) ---------------------------------------------


def test_jsonl_event_emitter_appends_envelope(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    emit = jsonl_event_emitter(log_path)
    row = {
        "audit_id": "ffaud_abc",
        "occurred_at": "2026-04-25T17:30:00Z",
        "key": "anti_gaming.enabled",
        "flag_type": "boolean",
        "old_value": True,
        "new_value": False,
        "actor": "ops",
        "source": "cli",
        "reason": "test",
    }
    emit(row)
    persisted = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert persisted["event_name"] == "decision_policy_flag_changed"
    assert persisted["restricted"] is True
    assert persisted["audit_id"] == "ffaud_abc"
    assert persisted["key"] == "anti_gaming.enabled"


# ── governance YAML ----------------------------------------------------------


def test_governance_yaml_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The committed governance YAML must parse and validate."""
    ff = FeatureFlags(
        backend=NullBackend(),
        event_publisher=lambda row: None,
    )
    # Sanity-check a few of the registered keys.
    assert ff.spec("anti_gaming.enabled").restricted is True
    assert ff.spec("ui.show_decision_artifact_link").client_visible is True
