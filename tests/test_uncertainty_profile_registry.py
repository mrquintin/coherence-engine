"""Tests for uncertainty profile registry, manifest verification, and rollback semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.server.fund.services.uncertainty_profile_registry import (
    REGISTRY_SCHEMA_VERSION,
    RegistryError,
    empty_registry,
    export_runtime_profile_dict,
    load_registry,
    promote,
    read_profile_json,
    rollback,
    save_registry,
    verify_manifest_checksum,
)


def test_verify_manifest_checksum_ok():
    root = Path(__file__).resolve().parent.parent
    ds = root / "data" / "governed" / "uncertainty_historical_outcomes.jsonl"
    mf = root / "data" / "governed" / "uncertainty_historical_outcomes.manifest.json"
    digest = verify_manifest_checksum(ds, mf)
    assert len(digest) == 64


def test_verify_manifest_checksum_mismatch(tmp_path):
    ds = tmp_path / "d.jsonl"
    ds.write_text("{}\n", encoding="utf-8")
    mf = tmp_path / "m.json"
    mf.write_text(
        json.dumps(
            {
                "dataset": "d.jsonl",
                "algorithm": "sha256",
                "checksum_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="checksum mismatch"):
        verify_manifest_checksum(ds, mf)


def test_promote_then_rollback_restores(tmp_path):
    reg_path = tmp_path / "registry.json"
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"best_parameters": {"sigma0": 0.01}, "tag": "a"}), encoding="utf-8")
    b.write_text(json.dumps({"best_parameters": {"sigma0": 0.02}, "tag": "b"}), encoding="utf-8")

    promote(reg_path, "shadow", a, recorded_at="2026-01-01T00:00:00Z")
    r1 = load_registry(reg_path)
    assert r1["stages"]["shadow"]["active"]["profile"]["tag"] == "a"
    assert r1["stages"]["shadow"]["rollback_stack"] == []

    promote(reg_path, "shadow", b, recorded_at="2026-01-02T00:00:00Z")
    r2 = load_registry(reg_path)
    assert r2["stages"]["shadow"]["active"]["profile"]["tag"] == "b"
    assert len(r2["stages"]["shadow"]["rollback_stack"]) == 1
    assert r2["stages"]["shadow"]["rollback_stack"][0]["profile"]["tag"] == "a"

    rollback(reg_path, "shadow")
    r3 = load_registry(reg_path)
    assert r3["stages"]["shadow"]["active"]["profile"]["tag"] == "a"
    assert r3["stages"]["shadow"]["rollback_stack"] == []


def test_rollback_empty_stack_fails(tmp_path):
    reg_path = tmp_path / "registry.json"
    with pytest.raises(RegistryError, match="rollback_stack is empty"):
        rollback(reg_path, "canary")


def test_double_rollback_second_fails(tmp_path):
    reg_path = tmp_path / "registry.json"
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"k": 1}), encoding="utf-8")
    b.write_text(json.dumps({"k": 2}), encoding="utf-8")
    promote(reg_path, "prod", a, recorded_at="t1")
    promote(reg_path, "prod", b, recorded_at="t2")
    rollback(reg_path, "prod")
    with pytest.raises(RegistryError, match="rollback_stack is empty"):
        rollback(reg_path, "prod")


def test_stages_are_independent(tmp_path):
    reg_path = tmp_path / "registry.json"
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"x": 1}), encoding="utf-8")
    promote(reg_path, "shadow", p, recorded_at="t0")
    promote(reg_path, "canary", p, recorded_at="t0")
    r = load_registry(reg_path)
    assert r["stages"]["shadow"]["active"] is not None
    assert r["stages"]["canary"]["active"] is not None
    assert r["stages"]["prod"]["active"] is None


def test_save_load_roundtrip_sorts_keys(tmp_path):
    reg_path = tmp_path / "r.json"
    data = empty_registry()
    data["stages"]["shadow"]["active"] = {
        "profile": {"z": 1, "a": 2},
        "recorded_at": "t",
        "source_path": "/x",
        "reason": None,
    }
    save_registry(reg_path, data)
    again = load_registry(reg_path)
    assert again["schema_version"] == REGISTRY_SCHEMA_VERSION
    raw = reg_path.read_text(encoding="utf-8")
    assert raw.index('"a"') < raw.index('"z"')


def test_export_runtime_profile_dict_from_calibration_shape():
    entry = {
        "profile": {
            "uncertainty_model_version": "fund-cs-superiority-v1",
            "best_parameters": {"sigma0": 0.04, "z95": 1.96},
        }
    }
    flat = export_runtime_profile_dict(entry)
    assert flat["sigma0"] == pytest.approx(0.04)


def test_read_profile_json_rejects_non_object(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(RegistryError, match="root must be a JSON object"):
        read_profile_json(p)
