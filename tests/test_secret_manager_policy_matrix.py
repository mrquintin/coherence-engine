"""Provider policy conformance contracts for CI matrix runs."""

from __future__ import annotations

import os

import pytest

from coherence_engine.server.fund.services.secret_manager import (
    SecretManagerError,
    validate_secret_manager_policy,
)


_ENV_KEYS = [
    "COHERENCE_FUND_SECRET_MANAGER_PROVIDER",
    "COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY",
    "COHERENCE_FUND_SECRET_MANAGER_ALLOW_STATIC_CREDENTIALS",
    "COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED",
    "COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF",
    "COHERENCE_FUND_SECRET_MANAGER_TOKEN_FIELD",
    "COHERENCE_FUND_AWS_REGION",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "COHERENCE_FUND_VAULT_ADDR",
    "COHERENCE_FUND_VAULT_TOKEN",
    "COHERENCE_FUND_VAULT_TOKEN_FILE",
    "COHERENCE_FUND_VAULT_ALLOW_INSECURE_HTTP",
]


def _clear_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _selected_provider() -> str:
    return os.getenv("COHERENCE_TEST_PROVIDER_PROFILE", "").strip().lower()


@pytest.mark.parametrize("provider", ["aws", "gcp", "vault"])
def test_provider_policy_profile_contracts(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    selected = _selected_provider()
    if selected and selected != provider:
        pytest.skip(f"profile '{selected}' selected for CI matrix")

    _clear_policy_env(monkeypatch)
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", provider)
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_ALLOW_STATIC_CREDENTIALS", "false")
    monkeypatch.setenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true")
    monkeypatch.setenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "coherence/fund/bootstrap-admin")
    monkeypatch.setenv("COHERENCE_FUND_SECRET_MANAGER_TOKEN_FIELD", "token")

    if provider == "aws":
        monkeypatch.setenv("COHERENCE_FUND_AWS_REGION", "us-east-1")
        # Valid workload-identity posture should pass.
        validate_secret_manager_policy()

        # Static credentials are blocked under strict policy.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_TEST")
        with pytest.raises(SecretManagerError):
            validate_secret_manager_policy()

    elif provider == "gcp":
        # Valid workload-identity posture should pass.
        validate_secret_manager_policy()

        # File-based static credentials are blocked under strict policy.
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/gcp-key.json")
        with pytest.raises(SecretManagerError):
            validate_secret_manager_policy()

    else:
        monkeypatch.setenv("COHERENCE_FUND_VAULT_ADDR", "https://vault.internal:8200")
        monkeypatch.setenv("COHERENCE_FUND_VAULT_TOKEN", "vault-token")
        # HTTPS + token should pass.
        validate_secret_manager_policy()

        # Insecure HTTP should fail strict policy.
        monkeypatch.setenv("COHERENCE_FUND_VAULT_ADDR", "http://vault.internal:8200")
        with pytest.raises(SecretManagerError):
            validate_secret_manager_policy()

