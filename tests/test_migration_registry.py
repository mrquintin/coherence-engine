"""Tests for the Postgres-parity migration auditor (prompt 21)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter

import pytest

from coherence_engine.deploy.scripts import audit_migrations_postgres_parity as auditor


HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "migrations")
ENGINE_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(ENGINE_DIR)
REAL_VERSIONS = os.path.join(ENGINE_DIR, "alembic", "versions")


def _expected_fixture_registry() -> dict:
    with open(os.path.join(FIXTURES, "expected_registry.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_audit_migrations_synthetic_fixtures_match_expected_registry():
    audits = auditor.audit_migrations(FIXTURES, repo_root=ENGINE_DIR)
    registry = auditor.build_registry(audits, audited_at="2026-01-01T00:00:00+00:00")
    expected = _expected_fixture_registry()
    assert registry == expected


def test_clean_revision_has_no_findings():
    path = os.path.join(FIXTURES, "20260101_000001_clean.py")
    audit = auditor.audit_one_file(path, repo_root=ENGINE_DIR)
    assert audit.errors == []
    assert audit.warnings == []
    assert audit.postgres_compatible is True
    assert audit.sqlite_compatible is True


def test_alter_column_server_default_none_marks_sqlite_incompatible():
    path = os.path.join(FIXTURES, "20260101_000002_alter_default_none.py")
    audit = auditor.audit_one_file(path, repo_root=ENGINE_DIR)
    assert audit.sqlite_compatible is False
    assert audit.postgres_compatible is True
    codes = [e.code for e in audit.errors]
    assert codes == ["alter_column_server_default_none_sqlite"]


def test_naked_boolean_emits_warning_only():
    path = os.path.join(FIXTURES, "20260101_000003_naked_boolean.py")
    audit = auditor.audit_one_file(path, repo_root=ENGINE_DIR)
    assert audit.errors == []
    codes = [w.code for w in audit.warnings]
    assert codes == ["boolean_no_server_default"]


def test_batch_alter_table_recorded_as_sqlite_only_pattern_not_defect():
    path = os.path.join(FIXTURES, "20260101_000004_batch_alter.py")
    audit = auditor.audit_one_file(path, repo_root=ENGINE_DIR)
    assert audit.errors == []
    assert audit.postgres_compatible is True
    assert audit.sqlite_compatible is True
    codes = {w.code for w in audit.warnings}
    assert codes == {"sqlite_only_pattern_batch_alter_table"}


def test_real_migration_registry_parses_and_matches_pinned_baseline():
    audits = auditor.audit_migrations(REAL_VERSIONS, repo_root=ENGINE_DIR)
    registry = auditor.build_registry(audits, audited_at="2026-01-01T00:00:00+00:00")
    assert registry["schema_version"] == auditor.SCHEMA_VERSION
    assert isinstance(registry["revisions"], list)
    assert len(registry["revisions"]) == len(audits)

    with open(os.path.join(FIXTURES, "real_audit_baseline.json"), "r", encoding="utf-8") as fh:
        baseline = json.load(fh)

    assert len(audits) == baseline["total_revisions"], (
        "Number of alembic revisions changed; update real_audit_baseline.json."
    )

    total_errors = sum(len(a.errors) for a in audits)
    total_warnings = sum(len(a.warnings) for a in audits)
    assert total_errors == baseline["total_errors"], (
        f"Expected {baseline['total_errors']} errors, got {total_errors}. "
        "If prompt 24 fixed migrations, update real_audit_baseline.json."
    )
    assert total_warnings == baseline["total_warnings"], (
        f"Expected {baseline['total_warnings']} warnings, got {total_warnings}."
    )

    error_codes = Counter(e.code for a in audits for e in a.errors)
    warning_codes = Counter(w.code for a in audits for w in a.warnings)
    assert dict(error_codes) == baseline["errors_by_code"]
    assert dict(warning_codes) == baseline["warnings_by_code"]

    revs_with_errors = sorted(a.revision for a in audits if a.errors)
    assert revs_with_errors == sorted(baseline["revisions_with_errors"])


def test_cli_db_audit_migrations_no_write_returns_0_when_real_tree_has_no_errors():
    cmd = [
        sys.executable,
        "-m",
        "coherence_engine",
        "db",
        "audit-migrations",
        "--versions-dir",
        os.path.join(ENGINE_DIR, "alembic", "versions"),
        "--audited-at",
        "2026-01-01T00:00:00+00:00",
        "--json",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == auditor.SCHEMA_VERSION
    assert payload["audited_at"] == "2026-01-01T00:00:00+00:00"


def test_cli_db_audit_migrations_clean_fixture_returns_0(tmp_path):
    clean_dir = tmp_path / "versions"
    clean_dir.mkdir()
    src = os.path.join(FIXTURES, "20260101_000001_clean.py")
    dst = clean_dir / "20260101_000001_clean.py"
    with open(src, "r", encoding="utf-8") as f:
        dst.write_text(f.read(), encoding="utf-8")

    cmd = [
        sys.executable,
        "-m",
        "coherence_engine",
        "db",
        "audit-migrations",
        "--versions-dir",
        str(clean_dir),
        "--audited-at",
        "2026-01-01T00:00:00+00:00",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_write_registry_overwrites_target(tmp_path):
    out = tmp_path / "registry.json"
    audits = auditor.audit_migrations(FIXTURES, repo_root=ENGINE_DIR)
    registry = auditor.build_registry(audits, audited_at="2026-01-01T00:00:00+00:00")
    out.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == _expected_fixture_registry()


def test_committed_registry_has_expected_schema_version():
    path = os.path.join(ENGINE_DIR, "data", "governed", "migration_registry.json")
    if not os.path.exists(path):
        pytest.skip("registry not yet written")
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["schema_version"] == auditor.SCHEMA_VERSION
    assert isinstance(payload["audited_at"], str) and payload["audited_at"]
    assert isinstance(payload["revisions"], list)
