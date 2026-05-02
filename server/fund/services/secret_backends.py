"""Pluggable backend adapters for runtime secret resolution.

The :class:`SecretBackend` Protocol is the seam every backend implements;
the high-level :class:`coherence_engine.server.fund.services.secret_manager.SecretManager`
composes one or more of these to resolve secret names without ever
caching values to disk.

Each backend exposes a ``name`` (used for audit logging — see
``SecretManager``'s resolution log) and the two methods ``get(name)`` and
``health()``. ``get`` returns ``None`` when the secret is absent (so the
resolver can fall through to the next backend); raising is reserved for
hard transport / configuration errors.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional, Protocol, runtime_checkable


class SecretBackendError(RuntimeError):
    """Raised when a backend cannot complete a secret read."""


@runtime_checkable
class SecretBackend(Protocol):
    """Protocol every backend implements.

    ``get(name)`` returns the resolved value or ``None`` if the named
    secret is not present in this backend (callers fall through to the
    next backend in the chain). ``health()`` is a cheap reachability
    probe that must NOT print or expose secret values.
    """

    name: str

    def get(self, name: str) -> Optional[str]: ...

    def health(self) -> bool: ...


class EnvBackend:
    """Default backend — reads from ``os.environ``.

    Always healthy; never raises. ``None`` is returned for unset names
    and for empty-string values (we treat empty as missing — secrets
    are never legitimately the empty string).
    """

    name = "env"

    def __init__(self, environ: Optional[dict] = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def get(self, name: str) -> Optional[str]:
        raw = self._environ.get(name)
        if raw is None:
            return None
        value = raw.strip()
        return value or None

    def health(self) -> bool:
        return True


class DopplerBackend:
    """Doppler API backend.

    Reads `DOPPLER_TOKEN` (a service-token), optionally
    ``DOPPLER_PROJECT`` and ``DOPPLER_CONFIG`` for service-token scope.
    Caches resolved values for 60s — short enough that operator-driven
    rotation (re-create-token-and-restart) propagates within a minute.
    """

    name = "doppler"
    CACHE_TTL_SECONDS = 60.0
    _API_ROOT = "https://api.doppler.com/v3"

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        project: Optional[str] = None,
        config: Optional[str] = None,
        http_fetch: Optional[Callable[[str, dict], dict]] = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._token = (token or os.environ.get("DOPPLER_TOKEN", "")).strip()
        self._project = (project or os.environ.get("DOPPLER_PROJECT", "")).strip()
        self._config = (config or os.environ.get("DOPPLER_CONFIG", "")).strip()
        self._http = http_fetch
        self._timeout = timeout_seconds
        self._cache: dict[str, tuple[float, Optional[str]]] = {}
        if not self._token:
            raise SecretBackendError(
                "doppler backend requires DOPPLER_TOKEN to be set"
            )

    def _fetch(self, name: str) -> Optional[str]:
        params = {"name": name}
        if self._project:
            params["project"] = self._project
        if self._config:
            params["config"] = self._config
        url = f"{self._API_ROOT}/configs/config/secret?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if self._http is not None:
            payload = self._http(url, headers)
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                raise SecretBackendError(f"doppler request failed: {exc}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise SecretBackendError(f"doppler request failed: {exc}") from exc
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise SecretBackendError(f"doppler invalid JSON response: {exc}") from exc
        value = payload.get("value", {}) if isinstance(payload, dict) else {}
        if not isinstance(value, dict):
            raise SecretBackendError("doppler payload missing 'value' object")
        raw_val = value.get("raw")
        if raw_val in (None, ""):
            return None
        if not isinstance(raw_val, str):
            raise SecretBackendError("doppler 'value.raw' is not a string")
        return raw_val

    def get(self, name: str) -> Optional[str]:
        now = time.monotonic()
        cached = self._cache.get(name)
        if cached is not None and now - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1]
        value = self._fetch(name)
        self._cache[name] = (now, value)
        return value

    def health(self) -> bool:
        try:
            self._fetch("__health_probe__")
            return True
        except SecretBackendError:
            return False


class HashicorpVaultBackend:
    """HashiCorp Vault KV v2 backend.

    Reads `VAULT_ADDR` + `VAULT_TOKEN`; optional `VAULT_KV_MOUNT`
    (default ``secret``) and `VAULT_KV_PATH` (default ``coherence``)
    point at a single KV-v2 path that holds the bundle of secret
    fields. Vault returns a lease metadata block — we cache the
    decoded fields for ``lease_duration`` seconds (capped to 300s).
    """

    name = "vault"
    DEFAULT_CACHE_SECONDS = 300.0

    def __init__(
        self,
        *,
        addr: Optional[str] = None,
        token: Optional[str] = None,
        mount: Optional[str] = None,
        path: Optional[str] = None,
        http_fetch: Optional[Callable[[str, str, dict], dict]] = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._addr = (addr or os.environ.get("VAULT_ADDR", "")).strip().rstrip("/")
        self._token = (token or os.environ.get("VAULT_TOKEN", "")).strip()
        self._mount = (mount or os.environ.get("VAULT_KV_MOUNT", "secret")).strip().strip("/")
        self._path = (path or os.environ.get("VAULT_KV_PATH", "coherence")).strip().strip("/")
        self._http = http_fetch
        self._timeout = timeout_seconds
        self._cache_data: dict[str, str] = {}
        self._cache_expiry: float = 0.0
        if not self._addr:
            raise SecretBackendError("vault backend requires VAULT_ADDR")
        if not self._token:
            raise SecretBackendError("vault backend requires VAULT_TOKEN")

    def _refresh(self) -> None:
        url = f"{self._addr}/v1/{self._mount}/data/{self._path}"
        headers = {"X-Vault-Token": self._token, "Accept": "application/json"}
        if self._http is not None:
            payload = self._http("GET", url, headers)
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as response:
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._cache_data = {}
                    self._cache_expiry = time.monotonic() + 30.0
                    return
                raise SecretBackendError(f"vault request failed: {exc}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise SecretBackendError(f"vault request failed: {exc}") from exc
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise SecretBackendError(f"vault invalid JSON response: {exc}") from exc
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        kv = data.get("data", {}) if isinstance(data, dict) else {}
        if not isinstance(kv, dict):
            raise SecretBackendError("vault KV v2 'data.data' malformed")
        self._cache_data = {str(k): str(v) for k, v in kv.items() if v is not None}
        lease = payload.get("lease_duration", 0) if isinstance(payload, dict) else 0
        ttl = float(lease) if isinstance(lease, (int, float)) and lease > 0 else self.DEFAULT_CACHE_SECONDS
        self._cache_expiry = time.monotonic() + min(ttl, self.DEFAULT_CACHE_SECONDS)

    def get(self, name: str) -> Optional[str]:
        if time.monotonic() >= self._cache_expiry:
            self._refresh()
        value = self._cache_data.get(name)
        if value in (None, ""):
            return None
        return value

    def health(self) -> bool:
        try:
            self._refresh()
            return True
        except SecretBackendError:
            return False


class SupabaseVaultBackend:
    """Supabase Vault backend via ``supabase.rpc('get_secret', ...)``.

    Requires a service-role Supabase client; the backing RPC must be
    defined server-side (canonical pattern: ``select decrypted_secret
    from vault.decrypted_secrets where name = $1``). Useful when the
    application already has Supabase admin access and prefers not to
    add a second secret-store dependency.
    """

    name = "supabase_vault"

    def __init__(
        self,
        *,
        client: object = None,
        url: Optional[str] = None,
        service_role_key: Optional[str] = None,
        rpc_name: str = "get_secret",
    ) -> None:
        self._rpc_name = rpc_name
        self._client = client
        if self._client is None:
            url = (url or os.environ.get("SUPABASE_URL", "")).strip()
            key = (service_role_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
            if not url or not key:
                raise SecretBackendError(
                    "supabase vault backend requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
                )
            try:
                from supabase import create_client  # type: ignore[import-not-found]
            except ImportError as exc:
                raise SecretBackendError(
                    "supabase python client is not installed"
                ) from exc
            self._client = create_client(url, key)

    def get(self, name: str) -> Optional[str]:
        try:
            response = self._client.rpc(self._rpc_name, {"secret_name": name}).execute()
        except Exception as exc:  # supabase wraps a wide range of errors
            raise SecretBackendError(f"supabase vault rpc failed: {exc}") from exc
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("data")
        if data in (None, "", []):
            return None
        if isinstance(data, list):
            data = data[0] if data else None
            if isinstance(data, dict):
                data = data.get("decrypted_secret") or data.get("value")
        if not isinstance(data, str) or not data.strip():
            return None
        return data.strip()

    def health(self) -> bool:
        try:
            self._client.rpc(self._rpc_name, {"secret_name": "__health_probe__"}).execute()
            return True
        except Exception:
            return False


def build_backend(name: str, **kwargs: object) -> SecretBackend:
    """Factory: instantiate a backend by short name.

    Recognized names: ``env``, ``doppler``, ``vault``, ``supabase_vault``.
    """
    key = name.strip().lower()
    if key in ("", "env", "environment"):
        return EnvBackend()
    if key == "doppler":
        return DopplerBackend(**kwargs)  # type: ignore[arg-type]
    if key in ("vault", "hashicorp_vault"):
        return HashicorpVaultBackend(**kwargs)  # type: ignore[arg-type]
    if key in ("supabase", "supabase_vault"):
        return SupabaseVaultBackend(**kwargs)  # type: ignore[arg-type]
    raise SecretBackendError(f"unknown secret backend: {name}")
