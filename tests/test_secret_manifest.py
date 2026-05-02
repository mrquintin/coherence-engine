"""Tests for the manifest schema and the SecretManager resolver chain.

The high-level :class:`SecretManager` composes pluggable backends and
verifies a declarative manifest at startup. Production env with any
``prod_required`` secret unresolved must raise
:class:`MissingRequiredSecret`.

CLI ``secrets resolve`` defense-in-depth (three concurrent flags) is
also exercised here as a black-box subprocess test — the prompt's
"refuses with a clear error" requirement.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coherence_engine.server.fund.services.secret_backends import (
    EnvBackend,
)
from coherence_engine.server.fund.services.secret_manager import SecretManager
from coherence_engine.server.fund.services.secret_manifest import (
    ManifestEntry,
    ManifestError,
    ManifestReport,
    MissingRequiredSecret,
    SCHEMA_VERSION,
    SecretManifest,
    default_manifest_path,
)


# ── manifest schema ────────────────────────────────────────────────


def test_default_manifest_loads() -> None:
    manifest = SecretManifest.default()
    assert manifest.schema_version == SCHEMA_VERSION
    assert len(manifest.entries) > 0


def test_manifest_path_resolves_inside_governed_dir() -> None:
    p = default_manifest_path()
    assert p.exists()
    assert p.parent.name == "governed"


def test_manifest_rejects_unknown_schema_version() -> None:
    with pytest.raises(ManifestError, match="schema_version"):
        SecretManifest.from_dict({"schema_version": "v999", "secrets": []})


def test_manifest_rejects_empty_secrets() -> None:
    with pytest.raises(ManifestError, match="non-empty"):
        SecretManifest.from_dict({"schema_version": SCHEMA_VERSION, "secrets": []})


def test_manifest_rejects_invalid_policy() -> None:
    with pytest.raises(ManifestError, match="policy"):
        SecretManifest.from_dict(
            {
                "schema_version": SCHEMA_VERSION,
                "secrets": [
                    {"name": "FOO", "category": "x", "policy": "always_required"}
                ],
            }
        )


def test_manifest_rejects_duplicate_names() -> None:
    with pytest.raises(ManifestError, match="duplicate"):
        SecretManifest.from_dict(
            {
                "schema_version": SCHEMA_VERSION,
                "secrets": [
                    {"name": "FOO", "category": "x", "policy": "prod_required"},
                    {"name": "FOO", "category": "y", "policy": "prod_optional"},
                ],
            }
        )


def test_manifest_entry_validates_required_fields() -> None:
    with pytest.raises(ManifestError, match="name"):
        ManifestEntry(name="", category="x", policy="prod_required")


# ── SecretManager (resolver chain) ─────────────────────────────────


def _toy_manifest() -> SecretManifest:
    return SecretManifest.from_dict(
        {
            "schema_version": SCHEMA_VERSION,
            "secrets": [
                {"name": "REQUIRED_ONE", "category": "db", "policy": "prod_required"},
                {"name": "REQUIRED_TWO", "category": "auth", "policy": "prod_required"},
                {"name": "OPTIONAL_ONE", "category": "kyc", "policy": "prod_optional"},
                {"name": "DEV_ONE", "category": "ops", "policy": "dev_optional"},
            ],
        }
    )


def test_resolver_caches_value_across_calls() -> None:
    backend = EnvBackend(environ={"FOO": "bar"})
    sm = SecretManager(primary=backend)
    assert sm.get("FOO") == "bar"
    # Mutate underlying environ to confirm the in-memory cache holds.
    backend._environ["FOO"] = "rotated"  # type: ignore[attr-defined]
    assert sm.get("FOO") == "bar"


def test_resolver_logs_backend_per_resolution() -> None:
    primary = EnvBackend(environ={"PRIMARY_ONLY": "p"})
    fallback = EnvBackend(environ={"FALLBACK_ONLY": "f"})

    # Fake a non-env primary so fallback is consulted.
    primary.name = "fakeprimary"  # type: ignore[attr-defined]
    sm = SecretManager(primary=primary, fallback=fallback)
    assert sm.get("PRIMARY_ONLY") == "p"
    assert sm.get("FALLBACK_ONLY") == "f"
    log = sm.resolution_log()
    assert ("PRIMARY_ONLY", "fakeprimary") in log
    assert ("FALLBACK_ONLY", "env") in log


def test_resolver_returns_none_when_all_backends_miss() -> None:
    sm = SecretManager(primary=EnvBackend(environ={}))
    assert sm.get("NOPE") is None


def test_verify_manifest_production_missing_required_raises() -> None:
    sm = SecretManager(
        primary=EnvBackend(environ={"REQUIRED_ONE": "x"}),  # REQUIRED_TWO absent
        manifest=_toy_manifest(),
    )
    with pytest.raises(MissingRequiredSecret) as excinfo:
        sm.verify_manifest("production")
    assert "REQUIRED_TWO" in excinfo.value.missing
    assert "REQUIRED_ONE" not in excinfo.value.missing


def test_verify_manifest_production_all_present_returns_report() -> None:
    sm = SecretManager(
        primary=EnvBackend(
            environ={
                "REQUIRED_ONE": "a",
                "REQUIRED_TWO": "b",
                # optional / dev_optional intentionally absent
            }
        ),
        manifest=_toy_manifest(),
    )
    report = sm.verify_manifest("production")
    assert isinstance(report, ManifestReport)
    assert report.missing_required == []
    assert report.resolved_count == 2
    assert report.missing_count == 2


def test_verify_manifest_dev_does_not_raise_on_missing_required() -> None:
    sm = SecretManager(
        primary=EnvBackend(environ={}),
        manifest=_toy_manifest(),
    )
    report = sm.verify_manifest("development")
    assert len(report.missing_required) == 2
    # No raise — dev environments produce a status report only.


def test_manifest_report_to_dict_has_no_secret_values() -> None:
    sm = SecretManager(
        primary=EnvBackend(environ={"REQUIRED_ONE": "REAL_SECRET_VALUE"}),
        manifest=_toy_manifest(),
    )
    report = sm.verify_manifest("development")
    serialized = json.dumps(report.to_dict())
    assert "REAL_SECRET_VALUE" not in serialized


# ── CLI ``secrets resolve`` defense-in-depth ───────────────────────


def _cli_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    for key in [
        "CONFIRM_PRINT_SECRET",
        "COHERENCE_FUND_ENV",
        "APP_ENV",
        "SECRETS_BACKEND",
    ]:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def _cli_invoke(args: list[str], env: dict) -> subprocess.CompletedProcess:
    repo_parent = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "coherence_engine"] + args,
        capture_output=True,
        text=True,
        cwd=str(repo_parent),
        env=env,
    )


def test_cli_secrets_resolve_refuses_without_unsafe_print_flag() -> None:
    proc = _cli_invoke(
        ["secrets", "resolve", "--name", "FOO"],
        _cli_env({"FOO": "bar"}),
    )
    assert proc.returncode != 0
    assert "allow-unsafe-print" in proc.stderr.lower()


def test_cli_secrets_resolve_refuses_without_confirm_env() -> None:
    proc = _cli_invoke(
        ["secrets", "resolve", "--name", "FOO", "--allow-unsafe-print"],
        _cli_env({"FOO": "bar"}),
    )
    assert proc.returncode != 0
    assert "CONFIRM_PRINT_SECRET" in proc.stderr


def test_cli_secrets_resolve_refuses_in_production() -> None:
    proc = _cli_invoke(
        ["secrets", "resolve", "--name", "FOO", "--allow-unsafe-print"],
        _cli_env(
            {
                "FOO": "bar",
                "CONFIRM_PRINT_SECRET": "YES",
                "COHERENCE_FUND_ENV": "production",
            }
        ),
    )
    assert proc.returncode != 0
    assert "production" in proc.stderr.lower()


def test_cli_secrets_resolve_prints_value_with_all_safety_gates() -> None:
    proc = _cli_invoke(
        ["secrets", "resolve", "--name", "FOO", "--allow-unsafe-print"],
        _cli_env(
            {
                "FOO": "the-test-value",
                "CONFIRM_PRINT_SECRET": "YES",
                "COHERENCE_FUND_ENV": "development",
            }
        ),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "the-test-value"


def test_cli_secrets_manifest_runs_without_values() -> None:
    proc = _cli_invoke(
        ["secrets", "manifest", "--env", "development"],
        _cli_env({"SUPABASE_DB_URL": "postgres://hidden:hidden@host/db"}),
    )
    assert proc.returncode == 0, proc.stderr
    # Value must NEVER appear in manifest output.
    assert "hidden" not in proc.stdout.lower()
    assert "schema=secret-manifest-v1" in proc.stdout
