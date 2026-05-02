"""Single source of truth for environment-conditional behavior.

Every place in the codebase that wants to do something different in
``prod`` than in ``dev`` reads from one of the gate functions here.
The gates **do not** consult ``os.environ`` directly — they read
:attr:`coherence_engine.server.fund.config.FundSettings.environment`,
which itself was resolved exactly once at startup.

Why have this module instead of ad-hoc ``if env == "prod"`` checks?

* It enumerates every legitimate behavior split in one file, so a
  reviewer can audit "what differs between staging and prod?" by
  reading this module alone.
* It defends against drift: ``is_prod()`` is the only correct way
  to ask the question, and any new gate needs to live here. The
  twelve-factor auditor flags ``os.environ.get("ENV"...)`` patterns
  outside ``config.py`` precisely because they bypass this layer.

All gate functions are pure reads of the settings object — they take
no arguments and have no side effects. They accept an optional
``settings`` parameter for tests that want to construct an isolated
``FundSettings`` instance instead of mutating the module-level one.
"""

from __future__ import annotations

from typing import Optional

from coherence_engine.server.fund.config import FundSettings, settings as _global_settings


def _s(settings: Optional[FundSettings]) -> FundSettings:
    return settings if settings is not None else _global_settings


# ── Environment identity ──────────────────────────────────────

def current_env(settings: Optional[FundSettings] = None) -> str:
    """Return the canonical environment token: dev|test|staging|prod."""
    return _s(settings).environment


def is_dev(settings: Optional[FundSettings] = None) -> bool:
    return _s(settings).environment == "dev"


def is_test(settings: Optional[FundSettings] = None) -> bool:
    return _s(settings).environment == "test"


def is_staging(settings: Optional[FundSettings] = None) -> bool:
    return _s(settings).environment == "staging"


def is_prod(settings: Optional[FundSettings] = None) -> bool:
    return _s(settings).environment == "prod"


def is_staging_or_prod(settings: Optional[FundSettings] = None) -> bool:
    return _s(settings).environment in ("staging", "prod")


def is_nonprod(settings: Optional[FundSettings] = None) -> bool:
    return _s(settings).environment != "prod"


# ── Behavior gates ────────────────────────────────────────────
#
# Each gate documents *why* the split exists. Add new gates only when
# the behavior genuinely differs between envs — otherwise expose the
# value as a Settings field and read it directly.

def allow_dry_run_backends(settings: Optional[FundSettings] = None) -> bool:
    """Permit storage / notification backends to no-op in dry-run mode.

    Useful in dev/test where we don't want to wire up a real S3 bucket
    or Slack workspace. Disabled in staging/prod so silent no-ops can
    never mask a real misconfiguration.
    """
    return _s(settings).environment in ("dev", "test")


def allow_debug_routes(settings: Optional[FundSettings] = None) -> bool:
    """Permit /debug/* HTTP routes (introspection, fixture seeding, ...).

    These routes can leak internals; never expose them in staging/prod.
    """
    return _s(settings).environment in ("dev", "test")


def allow_local_storage_backend(settings: Optional[FundSettings] = None) -> bool:
    """Permit ``STORAGE_BACKEND=local`` (filesystem-backed object store).

    Local storage is fine for dev/test, but in prod the model validator
    on :class:`FundSettings` rejects it outright. Staging is allowed to
    use local only when explicitly opted in (we don't promote here —
    the validator runs only on prod).
    """
    return _s(settings).environment in ("dev", "test", "staging")


def allow_auto_create_tables(settings: Optional[FundSettings] = None) -> bool:
    """Permit SQLAlchemy's ``create_all`` at startup.

    Convenient in dev/test; forbidden in staging/prod where Alembic
    owns the schema and a stray ``create_all`` could mask a bad
    migration.
    """
    return _s(settings).environment in ("dev", "test")


def allow_print_secret_value(settings: Optional[FundSettings] = None) -> bool:
    """Permit ``cli secrets resolve --allow-unsafe-print`` to actually print.

    Always disallowed in prod regardless of any other env vars; in
    other envs the CLI still requires the operator confirmation flag.
    """
    return _s(settings).environment != "prod"


def require_https(settings: Optional[FundSettings] = None) -> bool:
    """Require HTTPS for all outbound webhooks / inbound traffic.

    Relaxed only in dev where self-signed local services are common.
    """
    return _s(settings).environment != "dev"


def strict_secret_resolution(settings: Optional[FundSettings] = None) -> bool:
    """Whether ``prod_required`` secrets must resolve at startup.

    Honored verbatim from settings, but always coerced to True in prod
    even if the operator forgot to flip the flag.
    """
    s = _s(settings)
    return s.secret_manager_strict_policy or s.environment == "prod"
