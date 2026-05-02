"""Tests for the twelve-factor compliance auditor + env_gates + Settings."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest


# ── Load the auditor by file path so the test does not depend on
# ── deploy/scripts being importable as a package.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AUDITOR_PATH = _REPO_ROOT / "deploy" / "scripts" / "audit_twelve_factor.py"


def _load_auditor():
    mod_name = "_audit_twelve_factor_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _AUDITOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


audit_twelve_factor = _load_auditor()


# ── Fixture-based unit tests ──────────────────────────────────

GOOD_FIXTURE = textwrap.dedent('''
    """A well-behaved module."""
    import logging

    from coherence_engine.server.fund.config import settings

    log = logging.getLogger(__name__)


    def make_request():
        log.info("calling")
        return settings.DATABASE_URL
''').strip()


BAD_FIXTURE = textwrap.dedent('''
    import os
    import requests


    def fetch():
        api = os.environ.get("HARDCODED_API_KEY", "")
        host = os.getenv("DB_HOST")
        url = "https://api.evil.example.org/v1/things"
        print("fetching", url)
        return requests.get(url, headers={"X-Api-Key": api}).json()
''').strip()


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_audit_clean_fixture_yields_no_findings(tmp_path: Path):
    _write(tmp_path, "server/fund/clean.py", GOOD_FIXTURE)
    report = audit_twelve_factor.audit(tmp_path)
    payload = report.to_dict()
    # No findings of any severity for the clean fixture.
    assert payload["summary"]["total"] == 0
    assert payload["findings"] == []


def test_audit_bad_fixture_flags_each_violation_class(tmp_path: Path):
    _write(tmp_path, "server/fund/bad.py", BAD_FIXTURE)
    report = audit_twelve_factor.audit(tmp_path)
    rules = {f.rule for f in report.findings}
    # Each of the four rule families must fire on this fixture.
    assert "env_read_outside_config" in rules
    assert "hardcoded_url" in rules
    assert "print_in_runtime_code" in rules
    assert "http_call_without_timeout" in rules


def test_env_reads_in_config_layer_are_allowed(tmp_path: Path):
    # The auditor's allowlist names server/fund/config.py — env reads
    # there should not be reported.
    _write(tmp_path, "server/fund/config.py", "import os\nX = os.getenv('FOO', '')\n")
    report = audit_twelve_factor.audit(tmp_path)
    assert all(f.rule != "env_read_outside_config" for f in report.findings)


def test_env_reads_in_deploy_scripts_are_warn_not_error(tmp_path: Path):
    _write(tmp_path, "deploy/scripts/example.py", "import os\nX = os.getenv('FOO', '')\n")
    report = audit_twelve_factor.audit(tmp_path)
    env_findings = [f for f in report.findings if f.rule == "env_read_outside_config"]
    assert env_findings, "expected at least one env_read finding"
    assert all(f.severity == "warn" for f in env_findings)


def test_audit_report_payload_shape():
    report = audit_twelve_factor.AuditReport()
    report.findings.append(audit_twelve_factor.Finding(
        rule="r", severity="error", path="x.py", line=1, col=0, message="m",
    ))
    payload = report.to_dict()
    assert payload["schema_version"] == "1"
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["by_severity"]["error"] == 1


# ── Settings + env_gates ──────────────────────────────────────

def _fresh_settings(monkeypatch, **env):
    """Construct a fresh FundSettings under a controlled environ."""
    # Clear all env vars the model reads, then apply overrides.
    for k in (
        "COHERENCE_FUND_ENV", "APP_ENV",
        "COHERENCE_FUND_DATABASE_URL", "DATABASE_URL",
        "STORAGE_BACKEND", "COHERENCE_FUND_AUTO_CREATE_TABLES",
        "COHERENCE_FUND_SECRET_MANAGER_PROVIDER",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from coherence_engine.server.fund.config import FundSettings
    return FundSettings()


def test_environment_resolves_dev_by_default(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.environment == "dev"


def test_environment_aliases_normalize(monkeypatch):
    s = _fresh_settings(monkeypatch, COHERENCE_FUND_ENV="production",
                        STORAGE_BACKEND="s3",
                        COHERENCE_FUND_AUTO_CREATE_TABLES="false",
                        COHERENCE_FUND_DATABASE_URL="postgresql://x/y",
                        COHERENCE_FUND_SECRET_MANAGER_PROVIDER="env")
    assert s.environment == "prod"
    s2 = _fresh_settings(monkeypatch, APP_ENV="staging")
    assert s2.environment == "staging"


def test_environment_rejects_unknown_value(monkeypatch):
    with pytest.raises(Exception):
        _fresh_settings(monkeypatch, COHERENCE_FUND_ENV="qa")


def test_is_prod_only_true_when_environment_is_prod(monkeypatch):
    from coherence_engine.server.fund.services import env_gates
    dev = _fresh_settings(monkeypatch, COHERENCE_FUND_ENV="dev")
    prod = _fresh_settings(
        monkeypatch,
        COHERENCE_FUND_ENV="prod",
        STORAGE_BACKEND="s3",
        COHERENCE_FUND_AUTO_CREATE_TABLES="false",
        COHERENCE_FUND_DATABASE_URL="postgresql://x/y",
        COHERENCE_FUND_SECRET_MANAGER_PROVIDER="env",
    )
    assert env_gates.is_prod(dev) is False
    assert env_gates.is_prod(prod) is True
    assert env_gates.is_staging_or_prod(prod) is True
    assert env_gates.allow_dry_run_backends(prod) is False
    assert env_gates.allow_dry_run_backends(dev) is True
    assert env_gates.allow_debug_routes(prod) is False
    assert env_gates.allow_print_secret_value(prod) is False


def test_settings_rejects_local_storage_in_prod(monkeypatch):
    with pytest.raises(Exception):
        _fresh_settings(
            monkeypatch,
            COHERENCE_FUND_ENV="prod",
            STORAGE_BACKEND="local",
            COHERENCE_FUND_AUTO_CREATE_TABLES="false",
            COHERENCE_FUND_DATABASE_URL="postgresql://x/y",
            COHERENCE_FUND_SECRET_MANAGER_PROVIDER="env",
        )


def test_settings_rejects_sqlite_in_prod(monkeypatch):
    with pytest.raises(Exception):
        _fresh_settings(
            monkeypatch,
            COHERENCE_FUND_ENV="prod",
            STORAGE_BACKEND="s3",
            COHERENCE_FUND_AUTO_CREATE_TABLES="false",
            COHERENCE_FUND_DATABASE_URL="sqlite:///./x.db",
            COHERENCE_FUND_SECRET_MANAGER_PROVIDER="env",
        )


def test_settings_rejects_disabled_secret_manager_in_prod(monkeypatch):
    with pytest.raises(Exception):
        _fresh_settings(
            monkeypatch,
            COHERENCE_FUND_ENV="prod",
            STORAGE_BACKEND="s3",
            COHERENCE_FUND_AUTO_CREATE_TABLES="false",
            COHERENCE_FUND_DATABASE_URL="postgresql://x/y",
            COHERENCE_FUND_SECRET_MANAGER_PROVIDER="disabled",
        )


def test_settings_rejects_auto_create_tables_in_prod(monkeypatch):
    with pytest.raises(Exception):
        _fresh_settings(
            monkeypatch,
            COHERENCE_FUND_ENV="prod",
            STORAGE_BACKEND="s3",
            COHERENCE_FUND_AUTO_CREATE_TABLES="true",
            COHERENCE_FUND_DATABASE_URL="postgresql://x/y",
            COHERENCE_FUND_SECRET_MANAGER_PROVIDER="env",
        )


def test_redacted_dict_hides_secret_values(monkeypatch):
    s = _fresh_settings(monkeypatch)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "topsecret")
    from coherence_engine.server.fund.config import FundSettings
    s = FundSettings()
    redacted = s.to_redacted_dict()
    assert redacted["supabase_service_role_key"] == "***REDACTED***"


def test_uppercase_aliases_preserve_legacy_api(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.DATABASE_URL == s.database_url
    assert s.WORKER_BACKEND == s.worker_backend
    assert s.SERVICE_NAME == s.service_name
