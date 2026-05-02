#!/usr/bin/env python3
"""Migration CI gate (prompt 24).

Drives a real Postgres database through ``alembic upgrade head`` →
``alembic downgrade -1`` → ``alembic upgrade head`` and asserts that the
schema returned by ``pg_dump --schema-only`` is byte-identical (modulo
whitespace, blank lines, ``--`` line comments, and pg_dump's randomized
``\\restrict`` / ``\\unrestrict`` tokens) between the first and final
``upgrade head`` states.

The gate is **only** activated when the environment variable
``MIGRATION_GATE_PG_URL`` is set. When it is missing the script exits
``0`` with a clear "Postgres CI gate not configured" message — local
developers without a Postgres handy do not get blocked, but CI (which
sets ``MIGRATION_GATE_PG_URL`` from a GitHub Actions ``services:
postgres`` block) still runs the full check.

Exit codes
----------

* ``0`` - upgrade/downgrade/upgrade cycle ran cleanly and the
  ``pg_dump`` schema diff was empty.
* ``1`` - a transient failure occurred (alembic invocation crashed,
  ``pg_dump`` could not connect, etc.). The reason is printed to stderr.
* ``2`` - the cycle ran but the schema diff was non-empty: at least one
  migration is **not** reversible. The diff body is printed to stdout
  so the operator can see exactly what drifted.

The script imports nothing from ``coherence_engine`` directly so it can
run in a minimal CI image (just ``python``, ``alembic``, ``psycopg2`` /
``psycopg``, and ``pg_dump`` from ``postgresql-client``). It writes
nothing to governed datasets and never touches a non-ephemeral database.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


_REPO_ROOT = Path(__file__).resolve().parents[2]


EXIT_OK = 0
EXIT_TRANSIENT = 1
EXIT_SCHEMA_DRIFT = 2


# --- Schema-diff normalization ----------------------------------------------

_COMMENT_LINE_RE = re.compile(r"^\s*--.*$")
_BLANK_LINE_RE = re.compile(r"^\s*$")
_PG_DUMP_RESTRICT_RE = re.compile(r"^\s*\\(?:un)?restrict\s+\S+\s*$")


def _normalize_pg_dump(text: str) -> List[str]:
    """Strip comments, blank lines, and incidental whitespace from a pg_dump.

    ``pg_dump --schema-only`` emits comment metadata and, on newer client
    versions, randomized ``\\restrict`` / ``\\unrestrict`` guard tokens even
    when the schema is identical. Two dumps of the same schema differ only in
    those lines, so we strip them before comparing. Object ordering inside
    the dump is otherwise deterministic for a given Postgres major version
    (the gate's GitHub Actions job pins PG 15).
    """

    out: List[str] = []
    for raw in text.splitlines():
        if _COMMENT_LINE_RE.match(raw):
            continue
        if _BLANK_LINE_RE.match(raw):
            continue
        if _PG_DUMP_RESTRICT_RE.match(raw):
            continue
        out.append(raw.rstrip())
    return out


def _diff_lines(a: List[str], b: List[str]) -> List[str]:
    """Return a unified-style diff of two normalized line lists."""

    import difflib

    return list(
        difflib.unified_diff(a, b, fromfile="initial", tofile="final", lineterm="")
    )


# --- Subprocess helpers ------------------------------------------------------

def _run(
    cmd: List[str],
    *,
    env: Optional[dict] = None,
    cwd: Optional[Path] = None,
    capture: bool = False,
) -> Tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr).

    Wrapped so the unit tests can monkeypatch a single ``subprocess.run``
    seam — the production code path simply forwards into ``subprocess.run``.
    """

    completed = subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        check=False,
        capture_output=capture,
        text=capture,
    )
    return (
        completed.returncode,
        completed.stdout if capture else "",
        completed.stderr if capture else "",
    )


def _alembic(args: List[str], pg_url: str) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = pg_url
    env["COHERENCE_FUND_DATABASE_URL"] = pg_url
    env["SUPABASE_DB_URL"] = pg_url
    cmd = [sys.executable, "-m", "alembic"] + args
    return _run(cmd, env=env, cwd=_REPO_ROOT, capture=True)


def _pg_dump_schema(pg_url: str) -> Tuple[int, str, str]:
    if shutil.which("pg_dump") is None:
        return (1, "", "pg_dump binary not found on PATH")
    cmd = ["pg_dump", "--schema-only", "--no-owner", "--no-privileges", pg_url]
    return _run(cmd, capture=True)


# --- Gate body ---------------------------------------------------------------

def run_gate(pg_url: str, *, alembic_runner=_alembic, dump_runner=_pg_dump_schema) -> Tuple[int, str]:
    """Execute the upgrade/downgrade/upgrade reversibility cycle.

    Returns ``(exit_code, message)``. ``alembic_runner`` and
    ``dump_runner`` are seams the unit tests use to script subprocess
    behavior without standing up a real Postgres.
    """

    rc, _stdout, stderr = alembic_runner(["upgrade", "head"], pg_url)
    if rc != 0:
        return EXIT_TRANSIENT, f"initial alembic upgrade head failed: {stderr.strip()}"

    rc, initial_dump, stderr = dump_runner(pg_url)
    if rc != 0:
        return EXIT_TRANSIENT, f"initial pg_dump failed: {stderr.strip()}"

    rc, _stdout, stderr = alembic_runner(["downgrade", "-1"], pg_url)
    if rc != 0:
        return EXIT_TRANSIENT, f"alembic downgrade -1 failed: {stderr.strip()}"

    rc, _stdout, stderr = alembic_runner(["upgrade", "head"], pg_url)
    if rc != 0:
        return EXIT_TRANSIENT, f"second alembic upgrade head failed: {stderr.strip()}"

    rc, final_dump, stderr = dump_runner(pg_url)
    if rc != 0:
        return EXIT_TRANSIENT, f"final pg_dump failed: {stderr.strip()}"

    initial_lines = _normalize_pg_dump(initial_dump)
    final_lines = _normalize_pg_dump(final_dump)
    if initial_lines == final_lines:
        return EXIT_OK, "migration cycle reversible — schema diff is empty"

    diff = "\n".join(_diff_lines(initial_lines, final_lines))
    return EXIT_SCHEMA_DRIFT, f"schema drift detected after upgrade→downgrade→upgrade:\n{diff}"


# --- CLI --------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pg-url",
        default=None,
        help=(
            "Postgres URL to drive the migration cycle against. Defaults to "
            "$MIGRATION_GATE_PG_URL. When neither is set the gate exits 0 "
            "with a 'not configured' message."
        ),
    )
    parser.add_argument(
        "--require-configured",
        action="store_true",
        help=(
            "Treat a missing MIGRATION_GATE_PG_URL as an error (exit 1) "
            "instead of skipping. Used by CI workflows that must fail loudly "
            "if the secret was not wired up."
        ),
    )
    args = parser.parse_args(argv)

    pg_url = args.pg_url or os.environ.get("MIGRATION_GATE_PG_URL", "").strip()
    if not pg_url:
        msg = "Postgres CI gate not configured (MIGRATION_GATE_PG_URL unset) — skipping."
        if args.require_configured:
            print(msg, file=sys.stderr)
            return EXIT_TRANSIENT
        print(msg)
        return EXIT_OK

    exit_code, message = run_gate(pg_url)
    stream = sys.stderr if exit_code != EXIT_OK else sys.stdout
    print(message, file=stream)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
