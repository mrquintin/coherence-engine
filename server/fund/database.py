"""Database session and initialization helpers.

This module is dialect-aware: it selects between SQLite (default for unit
tests / dev), direct Postgres (any host), and Supabase Postgres reached via
the pgbouncer transaction-mode pooler. SQLite remains the default so that
``python -m pytest`` works with no environment configuration.

Resolution order in :func:`resolve_database_url`:

1. ``DATABASE_URL`` env (explicit override) — wins unconditionally.
2. ``SUPABASE_DB_POOLER_URL`` — pgbouncer pool URL for serverless / API workers.
3. ``SUPABASE_DB_URL`` — direct connection URL (Alembic / one-shot jobs).
4. ``COHERENCE_FUND_DATABASE_URL`` (legacy) → falls back to local SQLite.

Read-replica routing
--------------------

When ``SUPABASE_DB_REPLICA_URL`` is set, :func:`get_read_engine` returns an
engine bound to the replica with ``pool_pre_ping=True`` and roughly half of
the primary's pool size. :func:`SessionFactory` is a thin callable that
returns either a primary or read-replica session based on ``read_only``.

Transient-error retries
-----------------------

:func:`retry_transient_db_errors` is a decorator factory that retries a
small, well-known class of transient SQLAlchemy errors with bounded
exponential backoff and full jitter. Logic-bug errors (``IntegrityError``,
``DataError``) are never retried. The decorator accepts injectable
``sleeper`` / ``rng`` / ``logger`` so tests can be deterministic.

If the chosen URL points at a Supabase host (``*.supabase.co`` /
``*.supabase.com``), ``sslmode=require`` is appended to enforce TLS — Supabase
rejects unencrypted connections, but we make the requirement explicit in code
rather than relying on operator discipline.
"""

from __future__ import annotations

import functools
import logging
import os
import secrets
import time
from typing import Any, Callable, Generator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool



_SUPABASE_HOST_SUFFIXES = (".supabase.co", ".supabase.com")
_PGBOUNCER_HOST_HINT = "pooler"
_DEFAULT_SQLITE_URL = "sqlite:///./coherence_fund.db"

_LOG = logging.getLogger(__name__)

# Postgres SQLSTATEs we treat as transient and safe to retry:
#   40001 — serialization_failure
#   40P01 — deadlock_detected
#   57P01 — admin_shutdown / server going away
_TRANSIENT_PG_SQLSTATES = frozenset({"40001", "40P01", "57P01"})


class Base(DeclarativeBase):
    """Base declarative model."""


def _is_supabase_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    return any(host.endswith(sfx) for sfx in _SUPABASE_HOST_SUFFIXES)


def _ensure_sslmode_require(url: str) -> str:
    """Append ``sslmode=require`` to a Postgres URL if not already present.

    Only applied to Supabase hosts. Existing ``sslmode`` values (including
    stricter modes like ``verify-full``) are left untouched.
    """
    parsed = urlparse(url)
    if not _is_supabase_host(parsed.hostname):
        return url
    if not parsed.scheme.startswith("postgres"):
        return url
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "sslmode" in qs:
        return url
    qs["sslmode"] = "require"
    return urlunparse(parsed._replace(query=urlencode(qs)))


def _is_pooler_url(url: str) -> bool:
    """Return True if URL points at the Supabase pgbouncer pooler.

    Recognised by hostname containing ``pooler`` (e.g.
    ``aws-0-us-east-1.pooler.supabase.com``) or an explicit
    ``pgbouncer=true`` query parameter.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if _PGBOUNCER_HOST_HINT in host:
        return True
    qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return qs.get("pgbouncer", "").lower() == "true"


def resolve_database_url(env: dict[str, str] | None = None) -> str:
    """Resolve the active database URL according to the documented order.

    Parameters
    ----------
    env:
        Optional environment-variable mapping. When omitted, ``os.environ``
        is used. Tests inject custom mappings to avoid mutating the process
        environment.
    """
    src = os.environ if env is None else env

    explicit = src.get("DATABASE_URL", "").strip()
    if explicit:
        return _ensure_sslmode_require(explicit)

    pooler = src.get("SUPABASE_DB_POOLER_URL", "").strip()
    if pooler:
        return _ensure_sslmode_require(pooler)

    direct = src.get("SUPABASE_DB_URL", "").strip()
    if direct:
        return _ensure_sslmode_require(direct)

    legacy = src.get("COHERENCE_FUND_DATABASE_URL", "").strip()
    if legacy:
        return legacy

    return _DEFAULT_SQLITE_URL


def resolve_replica_database_url(env: dict[str, str] | None = None) -> str:
    """Resolve the read-replica database URL, or empty string if unset.

    Honors the same Supabase ``sslmode=require`` enforcement as the
    primary. Returning empty string signals that callers should fall back
    to the primary engine.
    """
    src = os.environ if env is None else env
    raw = src.get("SUPABASE_DB_REPLICA_URL", "").strip()
    if not raw:
        return ""
    return _ensure_sslmode_require(raw)


def _engine_kwargs_for_url(url: str) -> dict[str, Any]:
    """Return SQLAlchemy ``create_engine`` kwargs appropriate for the URL."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme.startswith("sqlite"):
        return {
            "future": True,
            "connect_args": {"check_same_thread": False},
        }

    if scheme.startswith("postgres"):
        if _is_pooler_url(url):
            # pgbouncer transaction-mode pools connections itself; layering
            # SQLAlchemy's pool on top causes prepared-statement / session
            # state corruption.
            return {
                "future": True,
                "poolclass": NullPool,
                "pool_pre_ping": True,
            }
        return {
            "future": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_pre_ping": True,
            "pool_recycle": 1800,
        }

    return {"future": True, "pool_pre_ping": True}


def _replica_engine_kwargs_for_url(url: str) -> dict[str, Any]:
    """Engine kwargs for the read-replica engine.

    Mirrors the primary's pool settings but halves ``pool_size`` and
    ``max_overflow`` for direct-Postgres URLs. SQLite + pgbouncer paths
    delegate to the primary kwargs verbatim — there's no useful "half" of
    a NullPool.
    """
    base = _engine_kwargs_for_url(url)
    if "pool_size" in base:
        base = dict(base)
        base["pool_size"] = max(1, int(base["pool_size"] // 2))
        if "max_overflow" in base:
            base["max_overflow"] = max(1, int(base["max_overflow"] // 2))
    return base


def safe_url_for_logging(url: str) -> str:
    """Return ``<dialect>://<user>@<host>/<db>`` with the password stripped."""
    parsed = urlparse(url)
    user = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    db = parsed.path.lstrip("/") if parsed.path else ""
    user_part = f"{user}@" if user else ""
    db_part = f"/{db}" if db else ""
    return f"{parsed.scheme}://{user_part}{host}{port}{db_part}"


def create_database_engine(url: str | None = None) -> Engine:
    """Build a SQLAlchemy engine for ``url`` (or the resolved URL)."""
    chosen = url or resolve_database_url()
    return create_engine(chosen, **_engine_kwargs_for_url(chosen))


def create_replica_engine(url: str) -> Engine:
    """Build a SQLAlchemy engine for the read replica at ``url``."""
    return create_engine(url, **_replica_engine_kwargs_for_url(url))


_RESOLVED_URL = resolve_database_url()
engine = create_database_engine(_RESOLVED_URL)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)

_REPLICA_ENGINE: Optional[Engine] = None
_REPLICA_ENGINE_URL: Optional[str] = None


def get_read_engine(env: dict[str, str] | None = None) -> Engine:
    """Return the read-replica engine, or the primary if unset.

    The replica engine is lazily created on first call and cached. When
    ``env`` is provided (tests), the cache is bypassed and a fresh
    decision is made — callers in production should not pass ``env``.
    """
    global _REPLICA_ENGINE, _REPLICA_ENGINE_URL

    replica_url = resolve_replica_database_url(env=env)
    if not replica_url:
        return engine

    if env is not None:
        # Test path: do not mutate the cached engine.
        return create_replica_engine(replica_url)

    if _REPLICA_ENGINE is not None and _REPLICA_ENGINE_URL == replica_url:
        return _REPLICA_ENGINE
    _REPLICA_ENGINE = create_replica_engine(replica_url)
    _REPLICA_ENGINE_URL = replica_url
    return _REPLICA_ENGINE


def SessionFactory(read_only: bool = False) -> Session:
    """Return a new session bound to primary or read-replica engine.

    A thin callable, NOT a class. ``read_only=True`` routes to whatever
    :func:`get_read_engine` resolves at call time, so flipping
    ``SUPABASE_DB_REPLICA_URL`` does not require a process restart for
    new sessions to honor the change (existing sessions continue to use
    the engine they were bound to).
    """
    target = get_read_engine() if read_only else engine
    factory = sessionmaker(
        bind=target,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return factory()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (starter migration path)."""
    from coherence_engine.server.fund import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


# ----------------------------------------------------------------------
# Transient-error retry decorator
# ----------------------------------------------------------------------


def _is_transient_db_error(exc: BaseException) -> bool:
    """Classify ``exc`` as a transient (retryable) database error.

    Logic-bug categories (``IntegrityError``, ``DataError``) are
    explicitly never transient — retrying them only hides bugs.
    """
    if isinstance(exc, (IntegrityError, DataError)):
        return False

    if isinstance(exc, DBAPIError):
        if getattr(exc, "connection_invalidated", False):
            return True
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode and str(pgcode) in _TRANSIENT_PG_SQLSTATES:
            return True

    if isinstance(exc, OperationalError):
        return True

    return False


def retry_transient_db_errors(
    *,
    max_attempts: int = 4,
    base_delay_ms: int = 50,
    max_delay_ms: int = 2000,
    sleeper: Callable[[float], None] | None = None,
    rng: Any | None = None,
    logger: logging.Logger | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory: retry transient DB errors with bounded backoff.

    Parameters
    ----------
    max_attempts:
        Total attempts including the first call. Must be >= 1.
    base_delay_ms / max_delay_ms:
        Full-jitter exponential backoff. The delay for retry ``n`` is
        ``uniform(0, min(max_delay_ms, base_delay_ms * 2**(n-1)))``.
        ``max_delay_ms`` is a hard cap.
    sleeper:
        Callable taking seconds-as-float. Defaults to :func:`time.sleep`.
        Tests inject a deterministic sleeper.
    rng:
        Object with a ``.uniform(a, b)`` method. Defaults to
        :class:`secrets.SystemRandom` — never :func:`random.random`,
        which is not collision-safe under contention.
    logger:
        Where to emit ``db.retry.attempt`` and ``db.retry.exhausted``
        structured logs. Defaults to this module's logger.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay_ms < 0 or max_delay_ms < 0:
        raise ValueError("delay bounds must be non-negative")
    if max_delay_ms < base_delay_ms:
        raise ValueError("max_delay_ms must be >= base_delay_ms")

    log = logger or _LOG
    sl = sleeper if sleeper is not None else time.sleep
    r = rng if rng is not None else secrets.SystemRandom()

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if not _is_transient_db_error(exc):
                        raise
                    if attempt >= max_attempts:
                        log.error(
                            "db.retry.exhausted",
                            extra={
                                "event": "db.retry.exhausted",
                                "function": fn.__name__,
                                "error_class": type(exc).__name__,
                                "attempts": attempt,
                                "last_error_message": str(exc)[:500],
                            },
                        )
                        raise
                    cap = min(max_delay_ms, base_delay_ms * (2 ** (attempt - 1)))
                    # Full-jitter: pick uniformly in [0, cap].
                    delay_ms = float(r.uniform(0.0, float(cap)))
                    log.warning(
                        "db.retry.attempt",
                        extra={
                            "event": "db.retry.attempt",
                            "function": fn.__name__,
                            "attempt": attempt,
                            "error_class": type(exc).__name__,
                            "delay_ms": delay_ms,
                        },
                    )
                    sl(delay_ms / 1000.0)

        return wrapper

    return decorator


__all__ = [
    "Base",
    "SessionLocal",
    "SessionFactory",
    "create_database_engine",
    "create_replica_engine",
    "engine",
    "get_db",
    "get_read_engine",
    "init_db",
    "resolve_database_url",
    "resolve_replica_database_url",
    "retry_transient_db_errors",
    "safe_url_for_logging",
]
