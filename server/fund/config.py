"""Configuration for production-grade fund backend.

This module is the *only* place ``os.environ`` is read for runtime
configuration. Every other module imports the resolved ``settings``
object below; downstream environment-conditional behavior goes through
:mod:`coherence_engine.server.fund.services.env_gates`.

The config layer is Pydantic v2 + ``pydantic-settings``. The single
source of truth for *which* environment we are running in is the
:attr:`FundSettings.environment` field — a strict
``Literal["dev","test","staging","prod"]`` resolved exactly once at
startup from ``COHERENCE_FUND_ENV`` / ``APP_ENV``. Hostname-based
auto-detection is explicitly **disallowed**.

Compatibility note: existing call sites read uppercase attributes
(``settings.DATABASE_URL``, ``settings.WORKER_BACKEND``, ...). Those
attributes are preserved by exposing each underlying field with an
uppercase property alias. The model itself is mutable so legacy
tests that monkeypatch fields keep working.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_log = logging.getLogger(__name__)


Environment = Literal["dev", "test", "staging", "prod"]


_ENV_ALIASES = {
    # accepted spellings -> canonical token
    "dev": "dev",
    "development": "dev",
    "local": "dev",
    "test": "test",
    "testing": "test",
    "ci": "test",
    "stage": "staging",
    "staging": "staging",
    "preprod": "staging",
    "pre-prod": "staging",
    "prod": "prod",
    "production": "prod",
}


def _resolve_environment_token() -> str:
    raw = (
        os.environ.get("COHERENCE_FUND_ENV")
        or os.environ.get("APP_ENV")
        or "dev"
    ).strip().lower()
    canonical = _ENV_ALIASES.get(raw)
    if canonical is None:
        raise ValueError(
            f"Invalid environment {raw!r}. "
            f"Set COHERENCE_FUND_ENV (or APP_ENV) to one of: dev, test, staging, prod."
        )
    return canonical


class FundSettings(BaseSettings):
    """Pydantic v2 settings model. Mutable by design."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        # Note: env vars are consumed via Field(default_factory=...) below
        # rather than the SettingsSource mechanism. This keeps the env-var
        # name mapping identical to the legacy class-attr layout.
    )

    # ── Environment taxonomy ────────────────────────────────────
    environment: Environment = Field(default_factory=_resolve_environment_token)

    # ── Database ────────────────────────────────────────────────
    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "COHERENCE_FUND_DATABASE_URL",
            "sqlite:///./coherence_fund.db",
        )
    )
    database_url_explicit: str = Field(
        default_factory=lambda: os.getenv("DATABASE_URL", "")
    )
    supabase_db_pooler_url: str = Field(
        default_factory=lambda: os.getenv("SUPABASE_DB_POOLER_URL", "")
    )
    supabase_db_url: str = Field(
        default_factory=lambda: os.getenv("SUPABASE_DB_URL", "")
    )
    supabase_db_replica_url: str = Field(
        default_factory=lambda: os.getenv("SUPABASE_DB_REPLICA_URL", "")
    )
    supabase_service_role_key: SecretStr = Field(
        default_factory=lambda: SecretStr(os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
    )

    # ── DB retry budget ─────────────────────────────────────────
    db_retry_max_attempts: int = Field(
        default_factory=lambda: int(os.getenv("COHERENCE_FUND_DB_RETRY_MAX_ATTEMPTS", "4") or "4")
    )
    db_retry_base_delay_ms: int = Field(
        default_factory=lambda: int(os.getenv("COHERENCE_FUND_DB_RETRY_BASE_DELAY_MS", "50") or "50")
    )
    db_retry_max_delay_ms: int = Field(
        default_factory=lambda: int(os.getenv("COHERENCE_FUND_DB_RETRY_MAX_DELAY_MS", "2000") or "2000")
    )

    # ── Service identity ────────────────────────────────────────
    event_schema_root: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_EVENT_SCHEMA_ROOT", "")
    )
    service_name: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_SERVICE_NAME", "fund-orchestrator-api")
    )
    service_version: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_SERVICE_VERSION", "0.2.0")
    )

    # ── Bootstrap / auth ────────────────────────────────────────
    auto_create_tables: bool = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_AUTO_CREATE_TABLES", "true").lower() == "true"
    )
    auth_mode: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_AUTH_MODE", "db")
    )
    secret_manager_provider: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "disabled")
    )
    secret_manager_strict_policy: bool = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true").lower() == "true"
    )
    secret_manager_startup_enforce: bool = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_SECRET_MANAGER_STARTUP_ENFORCE", "true").lower() == "true"
    )
    bootstrap_admin_enabled: bool = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true").lower() == "true"
    )
    bootstrap_admin_secret_ref: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "")
    )

    # ── Worker / queue ──────────────────────────────────────────
    worker_backend: str = Field(
        default_factory=lambda: os.getenv("WORKER_BACKEND", "poll").strip().lower()
    )
    redis_url: str = Field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )
    arq_queue_prefix: str = Field(
        default_factory=lambda: os.getenv("ARQ_QUEUE_PREFIX", "coherence_fund")
    )

    # ── Storage backend ─────────────────────────────────────────
    storage_backend: str = Field(
        default_factory=lambda: os.getenv("STORAGE_BACKEND", "local").strip().lower()
    )

    # ── API gateway: CORS / rate limit / signing (prompt 37) ───
    cors_allowed_origins: str = Field(
        default_factory=lambda: os.getenv("COHERENCE_FUND_CORS_ALLOWED_ORIGINS", "")
    )
    rate_limit_default: int = Field(
        default_factory=lambda: int(os.getenv("COHERENCE_FUND_RATE_LIMIT_DEFAULT", "120") or "120")
    )
    request_signing_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(os.getenv("COHERENCE_FUND_REQUEST_SIGNING_SECRET", ""))
    )
    request_signing_max_skew_seconds: int = Field(
        default_factory=lambda: int(os.getenv("COHERENCE_FUND_REQUEST_SIGNING_MAX_SKEW_SECONDS", "300") or "300")
    )

    # ── OpenTelemetry tracing (prompt 61) ───────────────────────
    # ``OTEL_*`` env vars match the OpenTelemetry standard names so any
    # collector / vendor that already understands OTel can be wired in
    # without a coherence-specific shim. ``otel_traces_exporter`` is a
    # comma-separated list (``otlp,console`` or ``none``); the actual
    # exporter wiring lives in :mod:`coherence_engine.server.fund.
    # observability.otel` and resolves the same env vars at runtime.
    otel_exporter_otlp_endpoint: str = Field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    )
    otel_traces_exporter: str = Field(
        default_factory=lambda: os.getenv("OTEL_TRACES_EXPORTER", "")
    )
    otel_service_name: str = Field(
        default_factory=lambda: os.getenv("OTEL_SERVICE_NAME", "")
    )

    # ── Cross-field invariants ─────────────────────────────────
    @model_validator(mode="after")
    def _enforce_prod_invariants(self) -> "FundSettings":
        if self.environment == "prod":
            if self.storage_backend == "local":
                raise ValueError(
                    "STORAGE_BACKEND=local is not allowed in production "
                    "(set STORAGE_BACKEND=s3 or supabase)."
                )
            if self.auto_create_tables:
                raise ValueError(
                    "COHERENCE_FUND_AUTO_CREATE_TABLES=true is not allowed in production "
                    "(migrations must run via Alembic)."
                )
            if self.database_url.startswith("sqlite:"):
                raise ValueError(
                    "sqlite database URL is not allowed in production."
                )
            if self.secret_manager_provider == "disabled":
                raise ValueError(
                    "COHERENCE_FUND_SECRET_MANAGER_PROVIDER=disabled is not allowed in production."
                )
        return self

    # ── Backwards-compatible UPPER_CASE alias accessors ────────
    # These keep ``settings.DATABASE_URL`` style reads working without
    # forcing a sweep of every call site at the same time as this audit.

    @property
    def DATABASE_URL(self) -> str: return self.database_url
    @DATABASE_URL.setter
    def DATABASE_URL(self, v: str) -> None: self.database_url = v

    @property
    def DATABASE_URL_EXPLICIT(self) -> str: return self.database_url_explicit
    @property
    def SUPABASE_DB_POOLER_URL(self) -> str: return self.supabase_db_pooler_url
    @property
    def SUPABASE_DB_URL(self) -> str: return self.supabase_db_url
    @property
    def SUPABASE_DB_REPLICA_URL(self) -> str: return self.supabase_db_replica_url
    @property
    def SUPABASE_SERVICE_ROLE_KEY(self) -> str:
        return self.supabase_service_role_key.get_secret_value()
    @property
    def DB_RETRY_MAX_ATTEMPTS(self) -> int: return self.db_retry_max_attempts
    @property
    def DB_RETRY_BASE_DELAY_MS(self) -> int: return self.db_retry_base_delay_ms
    @property
    def DB_RETRY_MAX_DELAY_MS(self) -> int: return self.db_retry_max_delay_ms
    @property
    def EVENT_SCHEMA_ROOT(self) -> str: return self.event_schema_root
    @property
    def SERVICE_NAME(self) -> str: return self.service_name
    @property
    def SERVICE_VERSION(self) -> str: return self.service_version
    @property
    def AUTO_CREATE_TABLES(self) -> bool: return self.auto_create_tables
    @property
    def AUTH_MODE(self) -> str: return self.auth_mode
    @property
    def SECRET_MANAGER_PROVIDER(self) -> str: return self.secret_manager_provider
    @property
    def SECRET_MANAGER_STRICT_POLICY(self) -> bool: return self.secret_manager_strict_policy
    @property
    def SECRET_MANAGER_STARTUP_ENFORCE(self) -> bool: return self.secret_manager_startup_enforce
    @property
    def BOOTSTRAP_ADMIN_ENABLED(self) -> bool: return self.bootstrap_admin_enabled
    @property
    def BOOTSTRAP_ADMIN_SECRET_REF(self) -> str: return self.bootstrap_admin_secret_ref
    @property
    def WORKER_BACKEND(self) -> str: return self.worker_backend
    @WORKER_BACKEND.setter
    def WORKER_BACKEND(self, v: str) -> None: self.worker_backend = str(v).strip().lower()
    @property
    def REDIS_URL(self) -> str: return self.redis_url
    @property
    def ARQ_QUEUE_PREFIX(self) -> str: return self.arq_queue_prefix
    @ARQ_QUEUE_PREFIX.setter
    def ARQ_QUEUE_PREFIX(self, v: str) -> None: self.arq_queue_prefix = str(v)
    @property
    def STORAGE_BACKEND(self) -> str: return self.storage_backend
    @property
    def CORS_ALLOWED_ORIGINS(self) -> str: return self.cors_allowed_origins
    @property
    def RATE_LIMIT_DEFAULT(self) -> int: return self.rate_limit_default
    @property
    def REQUEST_SIGNING_SECRET(self) -> str:
        return self.request_signing_secret.get_secret_value()
    @property
    def REQUEST_SIGNING_MAX_SKEW_SECONDS(self) -> int:
        return self.request_signing_max_skew_seconds

    @property
    def OTEL_EXPORTER_OTLP_ENDPOINT(self) -> str:
        return self.otel_exporter_otlp_endpoint
    @property
    def OTEL_TRACES_EXPORTER(self) -> str:
        return self.otel_traces_exporter
    @property
    def OTEL_SERVICE_NAME(self) -> str:
        return self.otel_service_name

    # ── Redacted dump for `config show` ─────────────────────────
    def to_redacted_dict(self) -> dict:
        out: dict = {}
        for name, _info in self.__class__.model_fields.items():
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                v = value.get_secret_value()
                out[name] = "***REDACTED***" if v else ""
            else:
                out[name] = value
        return out


settings = FundSettings()
