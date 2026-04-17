"""Configuration for production-grade fund backend."""

from __future__ import annotations

import os


class FundSettings:
    """Environment-backed settings."""

    DATABASE_URL: str = os.getenv(
        "COHERENCE_FUND_DATABASE_URL",
        "sqlite:///./coherence_fund.db",
    )
    EVENT_SCHEMA_ROOT: str = os.getenv(
        "COHERENCE_FUND_EVENT_SCHEMA_ROOT",
        "",
    )
    SERVICE_NAME: str = os.getenv("COHERENCE_FUND_SERVICE_NAME", "fund-orchestrator-api")
    SERVICE_VERSION: str = os.getenv("COHERENCE_FUND_SERVICE_VERSION", "0.2.0")
    AUTO_CREATE_TABLES: bool = os.getenv("COHERENCE_FUND_AUTO_CREATE_TABLES", "true").lower() == "true"
    AUTH_MODE: str = os.getenv("COHERENCE_FUND_AUTH_MODE", "db")
    SECRET_MANAGER_PROVIDER: str = os.getenv("COHERENCE_FUND_SECRET_MANAGER_PROVIDER", "disabled")
    SECRET_MANAGER_STRICT_POLICY: bool = (
        os.getenv("COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY", "true").lower() == "true"
    )
    SECRET_MANAGER_STARTUP_ENFORCE: bool = (
        os.getenv("COHERENCE_FUND_SECRET_MANAGER_STARTUP_ENFORCE", "true").lower() == "true"
    )
    BOOTSTRAP_ADMIN_ENABLED: bool = (
        os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED", "true").lower() == "true"
    )
    BOOTSTRAP_ADMIN_SECRET_REF: str = os.getenv("COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF", "")


settings = FundSettings()

