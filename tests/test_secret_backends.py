"""Backend-level tests for the runtime secret resolver (prompt 27).

Covers ``EnvBackend`` resolution, the in-process Doppler/Vault HTTP
seam (each backend takes a fake fetcher in its constructor — no real
network), the empty-string-as-missing convention, and the typed-error
surface on misconfiguration.

A separate ``test_secret_manifest.py`` covers manifest loading and the
high-level :class:`SecretManager` resolver chain.
"""

from __future__ import annotations

import pytest

from coherence_engine.server.fund.services.secret_backends import (
    DopplerBackend,
    EnvBackend,
    HashicorpVaultBackend,
    SecretBackendError,
    SupabaseVaultBackend,
    build_backend,
)


# ── EnvBackend ──────────────────────────────────────────────────────


def test_env_backend_returns_set_value() -> None:
    backend = EnvBackend(environ={"FOO": "bar"})
    assert backend.get("FOO") == "bar"
    assert backend.health() is True


def test_env_backend_returns_none_when_missing() -> None:
    backend = EnvBackend(environ={})
    assert backend.get("MISSING") is None


def test_env_backend_treats_empty_string_as_missing() -> None:
    backend = EnvBackend(environ={"FOO": "   "})
    assert backend.get("FOO") is None


def test_env_backend_strips_whitespace() -> None:
    backend = EnvBackend(environ={"FOO": "  value  "})
    assert backend.get("FOO") == "value"


# ── DopplerBackend ─────────────────────────────────────────────────


def test_doppler_backend_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)
    with pytest.raises(SecretBackendError, match="DOPPLER_TOKEN"):
        DopplerBackend()


def test_doppler_backend_returns_value_with_fake_fetch(monkeypatch) -> None:
    captured = {}

    def fake_fetch(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return {"value": {"raw": "the-secret-value"}}

    backend = DopplerBackend(token="dop_test_token", http_fetch=fake_fetch)
    assert backend.get("MY_SECRET") == "the-secret-value"
    assert "MY_SECRET" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer dop_test_token"


def test_doppler_backend_caches_for_60s(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_fetch(url, headers):
        calls["n"] += 1
        return {"value": {"raw": f"value-{calls['n']}"}}

    backend = DopplerBackend(token="dop_x", http_fetch=fake_fetch)
    assert backend.get("FOO") == "value-1"
    # Second call within TTL hits cache.
    assert backend.get("FOO") == "value-1"
    assert calls["n"] == 1


def test_doppler_backend_returns_none_on_empty_value() -> None:
    backend = DopplerBackend(
        token="dop_x",
        http_fetch=lambda url, headers: {"value": {"raw": ""}},
    )
    assert backend.get("EMPTY") is None


def test_doppler_backend_returns_none_when_value_missing() -> None:
    backend = DopplerBackend(
        token="dop_x",
        http_fetch=lambda url, headers: {"not_value": "oops"},
    )
    assert backend.get("FOO") is None


def test_doppler_backend_raises_when_value_is_not_an_object() -> None:
    backend = DopplerBackend(
        token="dop_x",
        http_fetch=lambda url, headers: {"value": "not-a-dict"},
    )
    with pytest.raises(SecretBackendError, match="missing 'value'"):
        backend.get("FOO")


# ── HashicorpVaultBackend ──────────────────────────────────────────


def test_vault_backend_requires_addr_and_token(monkeypatch) -> None:
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(SecretBackendError, match="VAULT_ADDR"):
        HashicorpVaultBackend()
    with pytest.raises(SecretBackendError, match="VAULT_TOKEN"):
        HashicorpVaultBackend(addr="https://vault.example")


def test_vault_backend_reads_kv_v2_with_fake_fetch() -> None:
    def fake_fetch(method, url, headers):
        assert method == "GET"
        assert "/v1/secret/data/coherence" in url
        assert headers["X-Vault-Token"] == "vault_test_token"
        return {
            "data": {"data": {"FOO": "bar", "BAZ": "qux"}},
            "lease_duration": 60,
        }

    backend = HashicorpVaultBackend(
        addr="https://vault.example",
        token="vault_test_token",
        http_fetch=fake_fetch,
    )
    assert backend.get("FOO") == "bar"
    assert backend.get("BAZ") == "qux"
    assert backend.get("MISSING") is None


def test_vault_backend_returns_none_for_unknown_keys() -> None:
    backend = HashicorpVaultBackend(
        addr="https://vault.example",
        token="vault_test_token",
        http_fetch=lambda m, u, h: {"data": {"data": {}}, "lease_duration": 60},
    )
    assert backend.get("ANYTHING") is None


# ── SupabaseVaultBackend ───────────────────────────────────────────


class _FakeSupabaseResponse:
    def __init__(self, data):
        self.data = data


class _FakeSupabaseClient:
    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        outer = self

        class _Q:
            def execute(self_inner):
                key = params["secret_name"]
                if key in outer._mapping:
                    return _FakeSupabaseResponse(outer._mapping[key])
                return _FakeSupabaseResponse(None)

        return _Q()


def test_supabase_vault_backend_returns_value_via_rpc() -> None:
    client = _FakeSupabaseClient({"FOO": "bar"})
    backend = SupabaseVaultBackend(client=client)
    assert backend.get("FOO") == "bar"
    assert client.calls == [("get_secret", {"secret_name": "FOO"})]


def test_supabase_vault_backend_returns_none_when_missing() -> None:
    client = _FakeSupabaseClient({})
    backend = SupabaseVaultBackend(client=client)
    assert backend.get("MISSING") is None


def test_supabase_vault_backend_unwraps_list_payload() -> None:
    client = _FakeSupabaseClient({"FOO": [{"decrypted_secret": "bar"}]})
    backend = SupabaseVaultBackend(client=client)
    assert backend.get("FOO") == "bar"


def test_supabase_vault_requires_url_and_key_when_no_client(monkeypatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(SecretBackendError, match="SUPABASE_URL"):
        SupabaseVaultBackend()


# ── factory ────────────────────────────────────────────────────────


def test_build_backend_env_default() -> None:
    backend = build_backend("env")
    assert isinstance(backend, EnvBackend)


def test_build_backend_unknown_raises() -> None:
    with pytest.raises(SecretBackendError, match="unknown"):
        build_backend("nope")
