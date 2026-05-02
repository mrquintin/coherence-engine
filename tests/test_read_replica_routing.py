"""Tests for read-replica engine resolution and SessionFactory routing.

These tests do NOT open a real Postgres connection — they exercise the
URL resolver and the engine factory against in-memory SQLite URLs that
stand in for primary / replica targets. The contract being verified:

* When ``SUPABASE_DB_REPLICA_URL`` is set, ``get_read_engine(env=...)``
  returns an engine bound to the replica URL, distinct from the
  primary.
* When unset, ``get_read_engine`` falls back to the primary engine.
* ``resolve_replica_database_url`` honors Supabase ``sslmode=require``
  enforcement and trims whitespace.
* The replica engine kwargs halve direct-Postgres pool sizes.
"""

from __future__ import annotations

import pytest

from coherence_engine.server.fund.database import (
    _replica_engine_kwargs_for_url,
    create_replica_engine,
    engine as primary_engine,
    get_read_engine,
    resolve_replica_database_url,
)


_SQLITE_PRIMARY = "sqlite:///:memory:"
_SQLITE_REPLICA = "sqlite:///./.replica.db"
_DIRECT_PG = "postgresql://user:pw@db.example.com:5432/app"
_REPLICA_SUPABASE = "postgresql://user:pw@db.replica.supabase.co:5432/postgres"
_POOLER_PG = "postgresql://user:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres"


# ----------------------------------------------------------------------
# resolve_replica_database_url
# ----------------------------------------------------------------------


def test_replica_url_unset_returns_empty_string():
    assert resolve_replica_database_url(env={}) == ""


def test_replica_url_blank_string_treated_as_unset():
    assert resolve_replica_database_url(env={"SUPABASE_DB_REPLICA_URL": "   "}) == ""


def test_replica_url_passthrough_for_generic_postgres():
    assert (
        resolve_replica_database_url(env={"SUPABASE_DB_REPLICA_URL": _DIRECT_PG})
        == _DIRECT_PG
    )


def test_replica_url_appends_sslmode_for_supabase_host():
    out = resolve_replica_database_url(env={"SUPABASE_DB_REPLICA_URL": _REPLICA_SUPABASE})
    assert out == _REPLICA_SUPABASE + "?sslmode=require"


def test_replica_url_preserves_existing_sslmode():
    url = _REPLICA_SUPABASE + "?sslmode=verify-full"
    assert resolve_replica_database_url(env={"SUPABASE_DB_REPLICA_URL": url}) == url


# ----------------------------------------------------------------------
# get_read_engine
# ----------------------------------------------------------------------


def test_get_read_engine_falls_back_to_primary_when_unset():
    assert get_read_engine(env={}) is primary_engine


def test_get_read_engine_returns_replica_when_set():
    replica = get_read_engine(env={"SUPABASE_DB_REPLICA_URL": _SQLITE_REPLICA})
    assert replica is not primary_engine
    # The replica engine's URL points at the replica we configured.
    assert str(replica.url) == _SQLITE_REPLICA


def test_get_read_engine_uses_supabase_sslmode():
    """The URL the engine factory consumes must already carry
    ``sslmode=require`` for Supabase replica hosts. We assert this at
    the resolver level so we don't need a real psycopg driver to
    actually instantiate the Postgres engine on the test runner."""
    resolved = resolve_replica_database_url(
        env={"SUPABASE_DB_REPLICA_URL": _REPLICA_SUPABASE}
    )
    assert "sslmode=require" in resolved


def test_get_read_engine_is_test_path_uncached():
    """``env=`` calls bypass the module-level cache so tests don't pollute
    one another."""
    a = get_read_engine(env={"SUPABASE_DB_REPLICA_URL": _SQLITE_REPLICA})
    b = get_read_engine(env={"SUPABASE_DB_REPLICA_URL": _SQLITE_REPLICA})
    # Two distinct engine instances — the test path always returns fresh.
    assert a is not b
    # Production-path call (env=None) with no replica configured returns primary.
    assert get_read_engine() is primary_engine


# ----------------------------------------------------------------------
# Replica engine kwargs
# ----------------------------------------------------------------------


def test_replica_kwargs_halve_direct_postgres_pool():
    kw = _replica_engine_kwargs_for_url(_DIRECT_PG)
    assert kw["pool_size"] == 5  # half of 10
    assert kw["max_overflow"] == 10  # half of 20
    assert kw["pool_pre_ping"] is True


def test_replica_kwargs_for_pgbouncer_match_primary():
    """NullPool has no useful 'half' — pgbouncer kwargs are unchanged."""
    from sqlalchemy.pool import NullPool

    kw = _replica_engine_kwargs_for_url(_POOLER_PG)
    assert kw["poolclass"] is NullPool
    assert "pool_size" not in kw


def test_replica_kwargs_for_sqlite_match_primary():
    kw = _replica_engine_kwargs_for_url(_SQLITE_PRIMARY)
    assert kw["future"] is True
    assert kw["connect_args"] == {"check_same_thread": False}


# ----------------------------------------------------------------------
# Repository routing — read methods route via SessionFactory(read_only=True)
# write methods stay on primary.
# ----------------------------------------------------------------------


def test_create_replica_engine_round_trip():
    """A replica engine can be instantiated standalone."""
    eng = create_replica_engine(_SQLITE_PRIMARY)
    assert eng is not None
    eng.dispose()


def test_repository_read_methods_route_to_replica(monkeypatch):
    """When SUPABASE_DB_REPLICA_URL is set, repository read methods that
    resolve through ``SessionFactory(read_only=True)`` open sessions
    bound to the replica engine. Write methods stay on the primary
    session passed to the constructor."""
    from coherence_engine.server.fund import database as db_mod
    from coherence_engine.server.fund.repositories.portfolio_repository import (
        PortfolioRepository,
    )

    # Build a sentinel "replica" engine and wire it in via SessionFactory.
    sentinel_replica = create_replica_engine(_SQLITE_PRIMARY)

    real_session_factory = db_mod.SessionFactory
    bound_engines: list[object] = []

    def fake_factory(read_only: bool = False):
        if read_only:
            from sqlalchemy.orm import Session, sessionmaker

            sess = sessionmaker(
                bind=sentinel_replica,
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
                class_=Session,
            )()
            bound_engines.append(sess.bind)
            return sess
        return real_session_factory(read_only=False)

    monkeypatch.setattr(db_mod, "SessionFactory", fake_factory)
    # Re-import the symbol used inside the package's __init__ helper.
    import coherence_engine.server.fund.repositories as repo_pkg

    monkeypatch.setattr(repo_pkg, "resolve_read_session", repo_pkg.resolve_read_session)

    # Repository constructed with NO primary session — every read MUST
    # route through the SessionFactory.
    repo = PortfolioRepository(db=None)
    # The schema for portfolio_state isn't installed on the sentinel
    # replica, so we expect this to raise — but the engine bound to the
    # session is the one we care about.
    with pytest.raises(Exception):
        repo.latest_state(read_only=True)

    assert bound_engines and bound_engines[0] is sentinel_replica
    sentinel_replica.dispose()


def test_repository_unset_replica_falls_back_to_primary(monkeypatch):
    """With SUPABASE_DB_REPLICA_URL unset, ``read_only=True`` opens a
    session against the primary engine."""
    from coherence_engine.server.fund import database as db_mod

    monkeypatch.delenv("SUPABASE_DB_REPLICA_URL", raising=False)

    sess = db_mod.SessionFactory(read_only=True)
    try:
        # Without a replica configured, the session must be bound to the
        # primary engine.
        assert sess.bind is db_mod.engine
    finally:
        sess.close()
