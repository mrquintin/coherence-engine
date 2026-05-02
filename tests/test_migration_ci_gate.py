"""Unit + integration tests for the migration CI gate (prompt 24).

The unit tests stub the alembic and pg_dump subprocess seams so they run
in any environment (no Postgres required). A single integration test,
gated by ``MIGRATION_GATE_PG_URL`` and the ``integration`` pytest marker,
exercises the full upgrade/downgrade/upgrade reversibility cycle against
a real Postgres if one is wired up.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = REPO_ROOT / "deploy" / "scripts" / "migration_ci_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("migration_ci_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_gate_module()


# --- Helpers to script the alembic / pg_dump seams --------------------------


def _make_alembic_runner(*, fail_on=None):
    """Return an alembic_runner stub that succeeds unless ``fail_on`` matches.

    ``fail_on`` is ``("upgrade", "head")`` style — when the args match the
    runner returns a non-zero exit code so we can drive the transient-error
    branches.
    """

    def runner(args, pg_url):
        if fail_on is not None and tuple(args) == tuple(fail_on):
            return (1, "", f"simulated failure on {args}")
        return (0, "", "")

    return runner


def _make_dump_runner(initial: str, final: str):
    """Return a dump_runner stub that yields ``initial`` then ``final``."""

    state = {"calls": 0}

    def runner(pg_url):
        state["calls"] += 1
        return (0, initial if state["calls"] == 1 else final, "")

    return runner


# --- Normalization ----------------------------------------------------------


def test_normalize_strips_comments_and_blank_lines(gate):
    raw = """\
-- PostgreSQL database dump

-- Dumped from database version 15.3
\\restrict randomTokenA
CREATE TABLE foo (id integer);

-- another comment
CREATE INDEX foo_id_idx ON foo(id);
\\unrestrict randomTokenA
"""
    out = gate._normalize_pg_dump(raw)
    assert out == ["CREATE TABLE foo (id integer);", "CREATE INDEX foo_id_idx ON foo(id);"]


def test_normalize_treats_only_comment_dumps_as_equal(gate):
    a = "-- header\n\nCREATE TABLE foo();"
    b = "-- different header line\n-- and another\n\nCREATE TABLE foo();"
    assert gate._normalize_pg_dump(a) == gate._normalize_pg_dump(b)


def test_normalize_ignores_pg_dump_restrict_tokens(gate):
    a = "\\restrict tokenA\nCREATE TABLE foo();\n\\unrestrict tokenA\n"
    b = "\\restrict tokenB\nCREATE TABLE foo();\n\\unrestrict tokenB\n"
    assert gate._normalize_pg_dump(a) == gate._normalize_pg_dump(b)


# --- run_gate happy path ----------------------------------------------------


def test_run_gate_returns_ok_when_dumps_match(gate):
    initial = "-- header A\nCREATE TABLE foo (id integer);\n"
    final = "-- header B (different)\nCREATE TABLE foo (id integer);\n"
    code, msg = gate.run_gate(
        "postgresql://x",
        alembic_runner=_make_alembic_runner(),
        dump_runner=_make_dump_runner(initial, final),
    )
    assert code == gate.EXIT_OK
    assert "reversible" in msg.lower()


def test_run_gate_detects_schema_drift(gate):
    initial = "CREATE TABLE foo (id integer);\n"
    final = "CREATE TABLE foo (id bigint);\n"
    code, msg = gate.run_gate(
        "postgresql://x",
        alembic_runner=_make_alembic_runner(),
        dump_runner=_make_dump_runner(initial, final),
    )
    assert code == gate.EXIT_SCHEMA_DRIFT
    assert "drift" in msg.lower()
    assert "bigint" in msg


# --- transient failure branches --------------------------------------------


@pytest.mark.parametrize(
    "fail_on,fragment",
    [
        (("upgrade", "head"), "initial alembic upgrade head failed"),
        (("downgrade", "-1"), "alembic downgrade -1 failed"),
    ],
)
def test_run_gate_reports_transient_alembic_failures(gate, fail_on, fragment):
    code, msg = gate.run_gate(
        "postgresql://x",
        alembic_runner=_make_alembic_runner(fail_on=fail_on),
        dump_runner=_make_dump_runner("a", "a"),
    )
    assert code == gate.EXIT_TRANSIENT
    assert fragment in msg


def test_run_gate_reports_transient_when_second_upgrade_fails(gate):
    """The second alembic upgrade head failure goes through the same branch as the first."""

    calls = {"n": 0}

    def runner(args, pg_url):
        if tuple(args) == ("upgrade", "head"):
            calls["n"] += 1
            if calls["n"] == 2:
                return (1, "", "second upgrade boom")
        return (0, "", "")

    code, msg = gate.run_gate(
        "postgresql://x",
        alembic_runner=runner,
        dump_runner=_make_dump_runner("a", "a"),
    )
    assert code == gate.EXIT_TRANSIENT
    assert "second alembic upgrade head failed" in msg


def test_run_gate_reports_transient_when_dump_fails(gate):
    def dump_runner(pg_url):
        return (1, "", "pg_dump explosion")

    code, msg = gate.run_gate(
        "postgresql://x",
        alembic_runner=_make_alembic_runner(),
        dump_runner=dump_runner,
    )
    assert code == gate.EXIT_TRANSIENT
    assert "pg_dump" in msg


# --- main() CLI surface -----------------------------------------------------


def test_main_skips_when_url_unset(gate, monkeypatch, capsys):
    monkeypatch.delenv("MIGRATION_GATE_PG_URL", raising=False)
    rc = gate.main([])
    captured = capsys.readouterr()
    assert rc == gate.EXIT_OK
    assert "not configured" in captured.out


def test_main_require_configured_fails_when_unset(gate, monkeypatch, capsys):
    monkeypatch.delenv("MIGRATION_GATE_PG_URL", raising=False)
    rc = gate.main(["--require-configured"])
    captured = capsys.readouterr()
    assert rc == gate.EXIT_TRANSIENT
    assert "not configured" in captured.err


def test_main_uses_pg_url_argument(gate, monkeypatch, capsys):
    monkeypatch.delenv("MIGRATION_GATE_PG_URL", raising=False)

    def fake_run_gate(pg_url):
        assert pg_url == "postgresql://supplied-via-flag/db"
        return (gate.EXIT_OK, "ok")

    monkeypatch.setattr(gate, "run_gate", fake_run_gate)
    rc = gate.main(["--pg-url", "postgresql://supplied-via-flag/db"])
    assert rc == gate.EXIT_OK


# --- Integration test (only runs if Postgres is actually available) --------


@pytest.mark.integration
def test_integration_reversibility_cycle_against_live_postgres(gate):
    pg_url = os.environ.get("MIGRATION_GATE_PG_URL", "").strip()
    if not pg_url:
        pytest.skip("MIGRATION_GATE_PG_URL not set — integration test skipped")
    code, msg = gate.run_gate(pg_url)
    assert code == gate.EXIT_OK, msg
