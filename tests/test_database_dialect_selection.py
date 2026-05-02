"""Tests for the dialect-aware database URL resolver and engine kwargs.

These tests are pure unit tests — they do NOT open a real database
connection. They assert that:

* ``resolve_database_url`` walks the documented precedence order
  (``DATABASE_URL`` > ``SUPABASE_DB_POOLER_URL`` > ``SUPABASE_DB_URL`` >
  legacy / SQLite default).
* The resolver appends ``sslmode=require`` when the URL targets a
  Supabase host, and leaves an existing ``sslmode`` value alone.
* ``_engine_kwargs_for_url`` returns the right pool configuration for
  SQLite, direct Postgres, and Supabase pgbouncer-pool URLs.
* ``safe_url_for_logging`` strips passwords.
"""

from __future__ import annotations

import pytest

from coherence_engine.server.fund.database import (
    _DEFAULT_SQLITE_URL,
    _engine_kwargs_for_url,
    resolve_database_url,
    safe_url_for_logging,
)
from sqlalchemy.pool import NullPool


_POOLER_URL = "postgresql://user:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
_DIRECT_SUPABASE_URL = "postgresql://user:pw@db.abcd1234.supabase.co:5432/postgres"
_GENERIC_PG_URL = "postgresql://user:pw@db.example.com:5432/app"
_SQLITE_URL = "sqlite:///./somewhere.db"


@pytest.mark.parametrize(
    "env,expected",
    [
        # Empty env -> SQLite default.
        ({}, _DEFAULT_SQLITE_URL),
        # Legacy var -> SQLite legacy.
        ({"COHERENCE_FUND_DATABASE_URL": _SQLITE_URL}, _SQLITE_URL),
        # Direct Supabase URL -> sslmode=require appended.
        (
            {"SUPABASE_DB_URL": _DIRECT_SUPABASE_URL},
            _DIRECT_SUPABASE_URL + "?sslmode=require",
        ),
        # Pooler URL -> sslmode=require appended.
        (
            {"SUPABASE_DB_POOLER_URL": _POOLER_URL},
            _POOLER_URL + "?sslmode=require",
        ),
        # Pooler beats direct.
        (
            {
                "SUPABASE_DB_POOLER_URL": _POOLER_URL,
                "SUPABASE_DB_URL": _DIRECT_SUPABASE_URL,
            },
            _POOLER_URL + "?sslmode=require",
        ),
        # Explicit DATABASE_URL beats everything.
        (
            {
                "DATABASE_URL": _GENERIC_PG_URL,
                "SUPABASE_DB_POOLER_URL": _POOLER_URL,
                "SUPABASE_DB_URL": _DIRECT_SUPABASE_URL,
            },
            _GENERIC_PG_URL,
        ),
    ],
)
def test_resolve_database_url_precedence(env, expected):
    assert resolve_database_url(env=env) == expected


def test_existing_sslmode_is_preserved():
    url = _DIRECT_SUPABASE_URL + "?sslmode=verify-full"
    assert resolve_database_url(env={"SUPABASE_DB_URL": url}) == url


def test_non_supabase_postgres_does_not_get_sslmode():
    assert resolve_database_url(env={"DATABASE_URL": _GENERIC_PG_URL}) == _GENERIC_PG_URL


def test_engine_kwargs_sqlite():
    kw = _engine_kwargs_for_url(_SQLITE_URL)
    assert kw["future"] is True
    assert kw["connect_args"] == {"check_same_thread": False}
    assert "pool_size" not in kw
    assert "poolclass" not in kw


def test_engine_kwargs_direct_postgres():
    kw = _engine_kwargs_for_url(_GENERIC_PG_URL)
    assert kw["future"] is True
    assert kw["pool_size"] == 10
    assert kw["max_overflow"] == 20
    assert kw["pool_pre_ping"] is True
    assert kw["pool_recycle"] == 1800
    assert "poolclass" not in kw


def test_engine_kwargs_supabase_pooler():
    kw = _engine_kwargs_for_url(_POOLER_URL)
    assert kw["future"] is True
    assert kw["poolclass"] is NullPool
    assert kw["pool_pre_ping"] is True
    assert "pool_size" not in kw


def test_engine_kwargs_pgbouncer_query_param():
    url = _GENERIC_PG_URL + "?pgbouncer=true"
    kw = _engine_kwargs_for_url(url)
    assert kw["poolclass"] is NullPool


def test_safe_url_for_logging_strips_password():
    safe = safe_url_for_logging(_DIRECT_SUPABASE_URL)
    assert "pw" not in safe
    assert "user@db.abcd1234.supabase.co:5432/postgres" in safe
    assert safe.startswith("postgresql://")


def test_safe_url_for_logging_sqlite():
    # SQLite URLs have no user/host — output should still be safe.
    safe = safe_url_for_logging(_SQLITE_URL)
    assert "pw" not in safe
    assert safe.startswith("sqlite://")
