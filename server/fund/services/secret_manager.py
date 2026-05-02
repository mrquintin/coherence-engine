"""Secret manager adapters for bootstrap auth and managed key rotation.

This module exposes two related but distinct surfaces:

* The legacy :class:`ManagedSecretStore` ABC and its concrete adapters
  (``AWSSecretsManager``, ``GCPSecretManager``, ``VaultKVv2SecretManager``)
  used to read/write the bootstrap-admin token from a managed store.
  The factory ``get_secret_manager()`` returns one of these.

* The runtime :class:`SecretManager` resolver — a thin compositor over
  pluggable :class:`~secret_backends.SecretBackend` implementations
  (env, Doppler, Vault, Supabase Vault). The resolver consults a
  declarative manifest (:mod:`secret_manifest`) at boot to verify
  every ``prod_required`` secret can be obtained before the app
  serves traffic.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from coherence_engine.server.fund.services.secret_backends import (
    EnvBackend,
    SecretBackend,
    SecretBackendError,
    build_backend,
)
from coherence_engine.server.fund.services.secret_manifest import (
    ManifestReport,
    MissingRequiredSecret,
    ResolvedEntry,
    SecretManifest,
)


_log = logging.getLogger(__name__)


class SecretManagerError(RuntimeError):
    """Raised when secret manager operations fail."""


class ManagedSecretStore:
    """Provider interface for managed secret reads/writes (legacy)."""

    def get_secret(self, secret_ref: str) -> str:
        raise NotImplementedError()

    def put_secret(self, secret_ref: str, secret_value: str) -> None:
        raise NotImplementedError()


def _timeout_seconds() -> float:
    raw = os.getenv("COHERENCE_FUND_SECRET_MANAGER_TIMEOUT_SECONDS", "5")
    try:
        return float(raw)
    except ValueError:
        return 5.0


def _token_field() -> str:
    return os.getenv("COHERENCE_FUND_SECRET_MANAGER_TOKEN_FIELD", "token")


def _provider() -> str:
    return os.getenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "disabled").strip().lower()


def _strict_policy_enabled() -> bool:
    return os.getenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true").strip().lower() == "true"


def _allow_static_credentials() -> bool:
    return os.getenv("COHERENCE_FUND_SECRET_MANAGER_ALLOW_STATIC_CREDENTIALS", "false").strip().lower() == "true"


def _bootstrap_admin_enabled() -> bool:
    return os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true").strip().lower() == "true"


def _bootstrap_admin_secret_ref() -> str:
    return os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "").strip()


def _vault_allow_insecure_http() -> bool:
    return os.getenv("COHERENCE_FUND_VAULT_ALLOW_INSECURE_HTTP", "false").strip().lower() == "true"


def _normalize_token(raw_secret: str) -> str:
    token = raw_secret.strip()
    if not token:
        raise SecretManagerError("empty secret value")
    try:
        parsed = json.loads(token)
    except json.JSONDecodeError:
        return token
    if isinstance(parsed, dict):
        candidate = parsed.get(_token_field())
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise SecretManagerError("secret JSON payload missing token field")


def _json_secret_payload(token: str) -> str:
    payload = {
        _token_field(): token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload)


def validate_secret_manager_policy() -> None:
    """Validate provider policy and credential posture before runtime operations."""
    provider = _provider()
    strict = _strict_policy_enabled()
    if provider in {"", "disabled", "none"}:
        if _bootstrap_admin_enabled() and _bootstrap_admin_secret_ref():
            raise SecretManagerError(
                "bootstrap admin secret is configured but secret manager provider is disabled"
            )
        return

    # Shared policy checks.
    if _bootstrap_admin_enabled() and not _bootstrap_admin_secret_ref():
        raise SecretManagerError("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF is required when bootstrap is enabled")
    if not _token_field().strip():
        raise SecretManagerError("COHERENCE_FUND_SECRET_MANAGER_TOKEN_FIELD must not be empty")

    if provider == "aws":
        region = os.getenv("COHERENCE_FUND_AWS_REGION") or os.getenv("AWS_REGION")
        if not region:
            raise SecretManagerError("AWS provider requires COHERENCE_FUND_AWS_REGION or AWS_REGION")
        if strict and not _allow_static_credentials():
            if os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_SECRET_ACCESS_KEY"):
                raise SecretManagerError(
                    "static AWS credentials are disallowed by strict policy; use workload identity/IAM role"
                )
    elif provider == "gcp":
        if strict and not _allow_static_credentials():
            if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                raise SecretManagerError(
                    "GOOGLE_APPLICATION_CREDENTIALS is disallowed by strict policy; use workload identity"
                )
    elif provider == "vault":
        addr = os.getenv("COHERENCE_FUND_VAULT_ADDR", "").strip()
        if not addr:
            raise SecretManagerError("vault provider requires COHERENCE_FUND_VAULT_ADDR")
        if strict and not _vault_allow_insecure_http() and addr.lower().startswith("http://"):
            raise SecretManagerError("vault provider requires HTTPS in strict policy mode")
        token = os.getenv("COHERENCE_FUND_VAULT_TOKEN", "").strip()
        token_file = os.getenv("COHERENCE_FUND_VAULT_TOKEN_FILE", "").strip()
        if not token and not token_file:
            raise SecretManagerError(
                "vault provider requires COHERENCE_FUND_VAULT_TOKEN or COHERENCE_FUND_VAULT_TOKEN_FILE"
            )
    else:
        raise SecretManagerError(f"unsupported secret manager provider: {provider}")


def probe_secret_manager_reachability(secret_ref: str) -> dict:
    """Connectivity probe used by startup and health endpoints."""
    provider = _provider()
    if provider in {"", "disabled", "none"}:
        return {
            "status": "disabled",
            "provider": "disabled",
            "reachable": False,
            "detail": "secret manager disabled",
        }

    manager = get_secret_manager()
    if manager is None:
        return {
            "status": "disabled",
            "provider": "disabled",
            "reachable": False,
            "detail": "secret manager disabled",
        }
    if not secret_ref:
        return {
            "status": "configured",
            "provider": provider,
            "reachable": False,
            "detail": "no secret_ref configured for active probe",
        }
    token = manager.get_secret(secret_ref)
    return {
        "status": "ready",
        "provider": provider,
        "reachable": True,
        "detail": f"secret_ref reachable ({secret_ref})",
        "fingerprint": _normalize_token(token)[:6],
    }


class AWSSecretsManager(ManagedSecretStore):
    """AWS Secrets Manager adapter."""

    def __init__(self):
        region = os.getenv("COHERENCE_FUND_AWS_REGION") or os.getenv("AWS_REGION") or "us-east-1"
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise SecretManagerError("boto3 is required for aws secret manager provider") from exc
        self._client = boto3.client("secretsmanager", region_name=region)

    def get_secret(self, secret_ref: str) -> str:
        try:
            res = self._client.get_secret_value(SecretId=secret_ref)
        except Exception as exc:
            raise SecretManagerError(f"aws get secret failed: {exc}") from exc
        secret_str = res.get("SecretString")
        if not isinstance(secret_str, str):
            raise SecretManagerError("aws secret is not a string")
        return _normalize_token(secret_str)

    def put_secret(self, secret_ref: str, secret_value: str) -> None:
        payload = _json_secret_payload(secret_value)
        try:
            self._client.put_secret_value(SecretId=secret_ref, SecretString=payload)
        except Exception as exc:
            raise SecretManagerError(f"aws put secret failed: {exc}") from exc


class GCPSecretManager(ManagedSecretStore):
    """GCP Secret Manager adapter via metadata + REST API."""

    TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
    API_ROOT = "https://secretmanager.googleapis.com/v1"

    def _metadata_access_token(self) -> str:
        # CI/CD can inject a short-lived access token directly.
        injected = os.getenv("COHERENCE_FUND_GCP_ACCESS_TOKEN", "").strip() or os.getenv("GCP_ACCESS_TOKEN", "").strip()
        if injected:
            return injected
        req = urllib.request.Request(
            self.TOKEN_URL,
            headers={"Metadata-Flavor": "Google"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=_timeout_seconds()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SecretManagerError(f"gcp metadata token fetch failed: {exc}") from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SecretManagerError("gcp metadata token missing access_token")
        return token

    def _request(self, method: str, url: str, body: Optional[dict] = None) -> dict:
        token = self._metadata_access_token()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_timeout_seconds()) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SecretManagerError(f"gcp secret manager request failed: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise SecretManagerError(f"gcp secret manager invalid JSON response: {exc}") from exc

    def get_secret(self, secret_ref: str) -> str:
        # secret_ref example: projects/<project>/secrets/<name>
        url = f"{self.API_ROOT}/{secret_ref}/versions/latest:access"
        payload = self._request("GET", url)
        data = payload.get("payload", {}).get("data")
        if not isinstance(data, str) or not data:
            raise SecretManagerError("gcp secret payload missing data")
        import base64

        try:
            decoded = base64.b64decode(data).decode("utf-8")
        except Exception as exc:
            raise SecretManagerError(f"gcp secret decode failed: {exc}") from exc
        return _normalize_token(decoded)

    def put_secret(self, secret_ref: str, secret_value: str) -> None:
        # Writes a new secret version.
        payload = _json_secret_payload(secret_value)
        import base64

        encoded = base64.b64encode(payload.encode("utf-8")).decode("utf-8")
        url = f"{self.API_ROOT}/{secret_ref}:addVersion"
        self._request("POST", url, {"payload": {"data": encoded}})


class VaultKVv2SecretManager(ManagedSecretStore):
    """Vault KV v2 adapter."""

    def __init__(self):
        self._addr = os.getenv("COHERENCE_FUND_VAULT_ADDR", "").rstrip("/")
        token = os.getenv("COHERENCE_FUND_VAULT_TOKEN", "").strip()
        token_file = os.getenv("COHERENCE_FUND_VAULT_TOKEN_FILE", "").strip()
        if not token and token_file:
            try:
                with open(token_file, "r", encoding="utf-8") as f:
                    token = f.read().strip()
            except OSError as exc:
                raise SecretManagerError(f"failed to read COHERENCE_FUND_VAULT_TOKEN_FILE: {exc}") from exc
        self._token = token
        if not self._addr:
            raise SecretManagerError("COHERENCE_FUND_VAULT_ADDR is required for vault provider")
        if not self._token:
            raise SecretManagerError("COHERENCE_FUND_VAULT_TOKEN is required for vault provider")

    def _split_ref(self, secret_ref: str) -> tuple[str, str]:
        # secret_ref example: secret/fund/bootstrap-admin
        parts = secret_ref.strip("/").split("/", 1)
        if len(parts) != 2:
            raise SecretManagerError("vault secret_ref must be '<mount>/<path>'")
        return parts[0], parts[1]

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self._addr}{path}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"X-Vault-Token": self._token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_timeout_seconds()) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SecretManagerError(f"vault request failed: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise SecretManagerError(f"vault invalid JSON response: {exc}") from exc

    def get_secret(self, secret_ref: str) -> str:
        mount, path = self._split_ref(secret_ref)
        payload = self._request("GET", f"/v1/{mount}/data/{path}")
        data = payload.get("data", {}).get("data", {})
        if not isinstance(data, dict):
            raise SecretManagerError("vault secret payload malformed")
        token = data.get(_token_field())
        if not isinstance(token, str) or not token:
            raise SecretManagerError("vault secret missing token field")
        return token

    def put_secret(self, secret_ref: str, secret_value: str) -> None:
        mount, path = self._split_ref(secret_ref)
        body = {"data": {_token_field(): secret_value}}
        self._request("POST", f"/v1/{mount}/data/{path}", body=body)


def get_secret_manager() -> Optional[ManagedSecretStore]:
    validate_secret_manager_policy()
    provider = _provider()
    if provider in {"", "disabled", "none"}:
        return None
    if provider == "aws":
        return AWSSecretsManager()
    if provider == "gcp":
        return GCPSecretManager()
    if provider == "vault":
        return VaultKVv2SecretManager()
    raise SecretManagerError(f"unsupported secret manager provider: {provider}")


# ---------------------------------------------------------------------------
# Runtime resolver (manifest-driven, pluggable backends).
# ---------------------------------------------------------------------------


def _runtime_env() -> str:
    return os.getenv("COHERENCE_FUND_ENV", os.getenv("APP_ENV", "development")).strip().lower()


def _primary_backend_name() -> str:
    return os.getenv("SECRETS_BACKEND", "env").strip().lower()


class SecretManager:
    """Runtime secret resolver over pluggable backends.

    Resolution order: ``primary`` first (configurable via the
    ``SECRETS_BACKEND`` env var — one of ``env``, ``doppler``, ``vault``,
    ``supabase_vault``), then ``EnvBackend`` as a fallback. Resolved
    values are cached in-memory for the process lifetime; rotation is
    handled by restart (documented in
    ``docs/specs/secret_management.md``).

    The resolver maintains an audit log of *which backend* resolved
    each name (never the value itself) so operators can trace
    surprising fallbacks. Secret values are never logged.
    """

    def __init__(
        self,
        *,
        primary: Optional[SecretBackend] = None,
        fallback: Optional[SecretBackend] = None,
        manifest: Optional[SecretManifest] = None,
    ) -> None:
        self._primary: SecretBackend = primary if primary is not None else EnvBackend()
        # If primary is already an EnvBackend, fallback is redundant.
        if fallback is None and self._primary.name != "env":
            fallback = EnvBackend()
        self._fallback: Optional[SecretBackend] = fallback
        self._manifest: Optional[SecretManifest] = manifest
        self._cache: dict[str, str] = {}
        self._resolution_log: list[tuple[str, str]] = []

    # -- factories -------------------------------------------------------

    @classmethod
    def from_env(cls, *, manifest: Optional[SecretManifest] = None) -> "SecretManager":
        """Build a resolver from environment configuration.

        Reads ``SECRETS_BACKEND`` (default ``env``). On backend
        construction failure, falls back to the env-only resolver and
        logs a warning — startup should not crash because a remote
        secret store is misconfigured; the manifest verification step
        is the real gate.
        """
        primary_name = _primary_backend_name()
        try:
            primary = build_backend(primary_name)
        except SecretBackendError as exc:
            _log.warning(
                "secrets: primary backend %r unavailable (%s); falling back to env",
                primary_name,
                exc,
            )
            primary = EnvBackend()
        return cls(primary=primary, manifest=manifest)

    # -- core resolution -------------------------------------------------

    def get(self, name: str) -> Optional[str]:
        """Return the resolved secret or ``None`` if no backend has it."""
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        chain: list[SecretBackend] = [self._primary]
        if self._fallback is not None and self._fallback is not self._primary:
            chain.append(self._fallback)
        for backend in chain:
            try:
                value = backend.get(name)
            except SecretBackendError as exc:
                _log.warning(
                    "secrets: backend %r raised on %s (%s); trying next",
                    backend.name,
                    name,
                    exc,
                )
                continue
            if value is None:
                continue
            self._cache[name] = value
            self._resolution_log.append((name, backend.name))
            _log.info("secrets: resolved name=%s backend=%s", name, backend.name)
            return value
        return None

    def resolution_log(self) -> list[tuple[str, str]]:
        return list(self._resolution_log)

    def manifest(self) -> SecretManifest:
        if self._manifest is None:
            self._manifest = SecretManifest.default()
        return self._manifest

    # -- manifest verification ------------------------------------------

    def verify_manifest(self, env: Optional[str] = None) -> ManifestReport:
        """Resolve every manifest entry and assemble a status report.

        For ``env="production"``, raises :class:`MissingRequiredSecret`
        if any ``prod_required`` entry could not be resolved. Other
        environments produce the same report but never raise — useful
        for dev/CI dashboards.
        """
        target_env = (env or _runtime_env()).strip().lower()
        manifest = self.manifest()
        entries: list[ResolvedEntry] = []
        for entry in manifest.entries:
            value = self.get(entry.name)
            if value is None:
                backend_name = None
                status = "missing"
            else:
                # Last item in resolution log is the most recent (this entry).
                backend_name = self._resolution_log[-1][1] if self._resolution_log else None
                status = "resolved"
            entries.append(
                ResolvedEntry(
                    name=entry.name,
                    category=entry.category,
                    policy=entry.policy,
                    status=status,
                    backend=backend_name,
                    owner=entry.owner,
                )
            )
        report = ManifestReport(
            env=target_env,
            schema_version=manifest.schema_version,
            entries=tuple(entries),
        )
        if target_env == "production" and report.missing_required:
            raise MissingRequiredSecret(report.missing_required)
        return report


_shared_resolver: Optional[SecretManager] = None


def get_secret_resolver() -> SecretManager:
    """Process-wide :class:`SecretManager` instance.

    Constructed lazily from environment configuration. Tests can pass
    a fresh instance to ``set_secret_resolver_for_tests``.
    """
    global _shared_resolver
    if _shared_resolver is None:
        _shared_resolver = SecretManager.from_env()
    return _shared_resolver


def set_secret_resolver_for_tests(resolver: Optional[SecretManager]) -> None:
    global _shared_resolver
    _shared_resolver = resolver

