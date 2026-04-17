"""Tests for the versioned prompt registry (Wave 1, prompt 08)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coherence_engine.server.fund.services.prompt_registry import (
    PromptRegistryError,
    default_registry_path,
    load_registry,
    registry_digest,
    resolve,
    verify_registry,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_fixture_registry(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write two body files and a registry JSON pointing at them.

    Returns (registry_path, body_a_path, body_b_path) as absolute paths;
    body_path fields in the registry are stored relative to tmp_path so that
    ``verify_registry(registry, repo_root=tmp_path)`` resolves them correctly.
    """
    bodies_dir = tmp_path / "bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    body_a = bodies_dir / "alpha.v1.md"
    body_b = bodies_dir / "beta.v1.md"
    body_a.write_bytes(b"# Alpha\n\nHello world.\n")
    body_b.write_bytes(b"# Beta\n\nCritique prompt body.\n")

    sha_a = hashlib.sha256(body_a.read_bytes()).hexdigest()
    sha_b = hashlib.sha256(body_b.read_bytes()).hexdigest()

    registry = {
        "schema_version": "prompt-registry-v1",
        "prompts": [
            {
                "id": "alpha",
                "version": "1.0.0",
                "status": "prod",
                "body_path": "bodies/alpha.v1.md",
                "content_sha256": sha_a,
                "owner": "test",
            },
            {
                "id": "beta",
                "version": "1.0.0",
                "status": "shadow",
                "body_path": "bodies/beta.v1.md",
                "content_sha256": sha_b,
                "owner": "test",
            },
        ],
    }
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return reg_path, body_a, body_b


def test_load_registry_parses_schema_and_entries(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)
    assert registry.schema_version == "prompt-registry-v1"
    assert len(registry.prompts) == 2
    ids = {e.id for e in registry.prompts}
    assert ids == {"alpha", "beta"}


def test_verify_registry_succeeds_on_fresh_fixtures(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)
    report = verify_registry(registry, repo_root=tmp_path)
    assert report.ok is True
    assert report.mismatches == []
    assert report.missing == []


def test_verify_registry_detects_body_mutation(tmp_path: Path):
    reg_path, body_a, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)

    body_a.write_bytes(body_a.read_bytes() + b"tamper\n")
    report = verify_registry(registry, repo_root=tmp_path)
    assert report.ok is False
    assert len(report.mismatches) == 1
    m = report.mismatches[0]
    assert m.prompt_id == "alpha"
    assert m.body_path == "bodies/alpha.v1.md"
    assert m.expected_sha256 != m.actual_sha256


def test_verify_registry_reports_missing_body(tmp_path: Path):
    reg_path, body_a, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)
    body_a.unlink()
    report = verify_registry(registry, repo_root=tmp_path)
    assert report.ok is False
    assert report.missing == ["bodies/alpha.v1.md"]


def test_registry_digest_is_deterministic_across_calls(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)
    d1 = registry_digest(registry)
    d2 = registry_digest(registry)
    assert d1 == d2
    assert len(d1) == 64
    int(d1, 16)  # valid hex


def test_registry_digest_changes_when_body_sha_changes(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    registry_before = load_registry(reg_path)
    digest_before = registry_digest(registry_before)

    data = json.loads(reg_path.read_text(encoding="utf-8"))
    data["prompts"][0]["content_sha256"] = "0" * 64
    reg_path.write_text(json.dumps(data), encoding="utf-8")

    registry_after = load_registry(reg_path)
    digest_after = registry_digest(registry_after)
    assert digest_before != digest_after


def test_registry_digest_insensitive_to_declared_entry_order(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    digest_original = registry_digest(load_registry(reg_path))

    data = json.loads(reg_path.read_text(encoding="utf-8"))
    data["prompts"].reverse()
    reg_path.write_text(json.dumps(data), encoding="utf-8")
    digest_reordered = registry_digest(load_registry(reg_path))

    assert digest_original == digest_reordered


def test_resolve_finds_entry(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)
    entry = resolve("alpha", "1.0.0", registry=registry)
    assert entry.id == "alpha"
    assert entry.version == "1.0.0"
    assert entry.status == "prod"


def test_resolve_missing_raises(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    registry = load_registry(reg_path)
    with pytest.raises(PromptRegistryError):
        resolve("alpha", "9.9.9", registry=registry)


def test_load_registry_rejects_unknown_schema_version(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    data["schema_version"] = "prompt-registry-v99"
    reg_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        load_registry(reg_path)


def test_load_registry_rejects_invalid_status(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    data["prompts"][0]["status"] = "bogus"
    reg_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        load_registry(reg_path)


def test_load_registry_rejects_duplicate_id_version(tmp_path: Path):
    reg_path, _, _ = _write_fixture_registry(tmp_path)
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    data["prompts"].append(dict(data["prompts"][0]))
    reg_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PromptRegistryError):
        load_registry(reg_path)


def test_shipped_registry_verifies_against_body_files():
    """The registry shipped with the repo must verify OK out of the box."""
    registry = load_registry(default_registry_path())
    report = verify_registry(registry)
    assert report.ok, f"shipped registry verification failed: {report.to_dict()}"
    assert len(registry.prompts) >= 2


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Invoke `python -m coherence_engine ...` with the repo root on sys.path."""
    env = os.environ.copy()
    parent = str(REPO_ROOT.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = parent + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "coherence_engine", "prompt-registry", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_verify_exits_zero_on_good_state():
    proc = _run_cli("verify")
    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_cli_list_prints_registered_prompts():
    proc = _run_cli("list")
    assert proc.returncode == 0, proc.stderr
    assert "interview_opening" in proc.stdout
    assert "self_critique" in proc.stdout


def test_cli_digest_prints_hex_sha256():
    proc = _run_cli("digest")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert len(out) == 64
    int(out, 16)  # valid hex


def test_cli_verify_exits_two_when_body_tampered(tmp_path: Path, monkeypatch):
    """Tamper with a copy of the registry+bodies in tmp_path and pass --registry."""
    reg_path, body_a, _ = _write_fixture_registry(tmp_path)
    body_a.write_bytes(body_a.read_bytes() + b"tamper\n")

    env = os.environ.copy()
    parent = str(REPO_ROOT.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = parent + (os.pathsep + existing if existing else "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "coherence_engine",
            "prompt-registry",
            "verify",
            "--registry",
            str(reg_path),
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2, (
        f"expected exit 2 on tamper, got {proc.returncode}: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
