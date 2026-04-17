"""Cloud-backed staging integration tests for secret-manager provider policy."""

from __future__ import annotations

import os

import pytest

try:  # pragma: no cover - import path differs between editable installs and repo-local runs
    from coherence_engine.server.fund.services.secret_manager import (
        probe_secret_manager_reachability,
        validate_secret_manager_policy,
    )
except ModuleNotFoundError:  # pragma: no cover
    from server.fund.services.secret_manager import (  # type: ignore
        probe_secret_manager_reachability,
        validate_secret_manager_policy,
    )


def _enabled() -> bool:
    return os.getenv("COHERENCE_CLOUD_SECRET_MANAGER_INTEGRATION", "").strip().lower() in {"1", "true", "yes"}


def _selected_provider() -> str:
    return os.getenv("COHERENCE_TEST_PROVIDER_PROFILE", "").strip().lower()


def _bootstrap_secret_ref() -> str:
    return os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "").strip()


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.fail(f"required env var is missing for cloud integration: {name}")
    return value


@pytest.mark.cloud_integration
@pytest.mark.parametrize("provider", ["aws", "gcp", "vault"])
def test_provider_policy_and_staging_reachability(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if not _enabled():
        pytest.skip("set COHERENCE_CLOUD_SECRET_MANAGER_INTEGRATION=1 to run cloud integration tests")

    selected = _selected_provider()
    if selected and selected != provider:
        pytest.skip(f"profile '{selected}' selected for cloud integration run")

    secret_ref = _bootstrap_secret_ref()
    if not secret_ref:
        pytest.fail("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF is required for cloud integration tests")

    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", provider)
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_ALLOW_STATIC_CREDENTIALS", "false")
    monkeypatch.setenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_TOKEN_FIELD", "token")

    if provider == "aws":
        # Required for deterministic provider policy validation; credentials come from OIDC/role chain.
        monkeypatch.setenv("COHERENCE_FUND_AWS_REGION", os.getenv("COHERENCE_FUND_AWS_REGION", "us-east-1"))
    elif provider == "gcp":
        # In CI, token should come from workload identity auth step.
        _require_env("COHERENCE_FUND_GCP_ACCESS_TOKEN")
    else:
        _require_env("COHERENCE_FUND_VAULT_ADDR")
        _require_env("COHERENCE_FUND_VAULT_TOKEN")

    validate_secret_manager_policy()
    probe = probe_secret_manager_reachability(secret_ref)
    assert probe["provider"] == provider
    assert probe["status"] == "ready"
    assert probe["reachable"] is True
