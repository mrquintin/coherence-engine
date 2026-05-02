"""Audit Alembic migrations for Postgres + SQLite parity (prompt 21).

Walks ``alembic/versions/``, parses each ``.py`` file with :mod:`ast`, and
records a deterministic per-revision parity status:

* ``op.alter_column(..., server_default=None)``
    SQLite barfs — recorded as an ``error`` and marks ``sqlite_compatible``
    false (Postgres still accepts it).
* ``sa.Boolean`` columns declared with ``nullable=False`` and no
    ``server_default``
    Recorded as a ``warning`` (``boolean_no_server_default``) — can wedge
    Postgres ``ALTER TABLE ... ADD COLUMN`` against a populated table.
* ``op.execute("...")`` containing dialect-specific SQL
    Regex-screen for ``PRAGMA`` (sqlite-only error) and ``IF NOT EXISTS``
    outside ``CREATE TABLE`` (postgres-only). Currently no migrations hit
    either case but the screen is in place for future revisions.
* ``op.batch_alter_table(...)`` blocks
    NOT a defect — recorded as a ``warning`` with code
    ``sqlite_only_pattern_batch_alter_table`` so prompt 24 can decide
    whether the block is redundant on Postgres.
* ``op.create_foreign_key(...)`` without a follow-up
    ``op.execute("ALTER TABLE ... NOT VALID")``
    Recorded as a ``warning`` (``fk_addition_no_not_valid``) — Postgres
    will hold a long ACCESS EXCLUSIVE lock on large tables otherwise.

The script never executes the migrations — it only parses their AST.

CLI surface (also exposed via ``python -m coherence_engine db
audit-migrations``)::

    python -m coherence_engine.deploy.scripts.audit_migrations_postgres_parity \
        [--versions-dir alembic/versions] \
        [--write-registry data/governed/migration_registry.json] \
        [--json] \
        [--audited-at 2026-04-18T14:02:30-05:00]

Exit code 0 = every revision has ``errors == []``; 2 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "migration-registry-v1"


@dataclass
class Finding:
    code: str
    message: str
    lineno: int

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "lineno": self.lineno}


@dataclass
class RevisionAudit:
    revision: str
    file: str
    postgres_compatible: bool = True
    sqlite_compatible: bool = True
    warnings: list[Finding] = field(default_factory=list)
    errors: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "file": self.file,
            "postgres_compatible": self.postgres_compatible,
            "sqlite_compatible": self.sqlite_compatible,
            "warnings": [w.to_dict() for w in self.warnings],
            "errors": [e.to_dict() for e in self.errors],
        }


_PRAGMA_RE = re.compile(r"\bPRAGMA\b", re.IGNORECASE)
_IF_NOT_EXISTS_RE = re.compile(r"\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE)
_CREATE_TABLE_RE = re.compile(r"\bCREATE\s+TABLE\b", re.IGNORECASE)
_NOT_VALID_RE = re.compile(r"\bNOT\s+VALID\b", re.IGNORECASE)


def _kwarg(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _attr_chain(node: ast.AST) -> str:
    """Return a dotted-name string for an attribute / name node, or ``""``."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _is_call_to(call: ast.Call, *, owner: str, name: str) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == name and _attr_chain(func.value).endswith(owner)
    return False


def _is_sa_type(call: ast.Call, type_name: str) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == type_name and _attr_chain(func.value).endswith("sa")
    if isinstance(func, ast.Name):
        return func.id == type_name
    return False


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_is_none(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _const_is_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _has_kw(call: ast.Call, name: str) -> bool:
    return _kwarg(call, name) is not None


def _walk_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _is_sa_column(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == "Column" and _attr_chain(func.value).endswith("sa")
    if isinstance(func, ast.Name):
        return func.id == "Column"
    return False


def _column_type_call(call: ast.Call) -> ast.Call | None:
    """Return the ``sa.<Type>(...)`` Call inside ``sa.Column(...)``."""
    for arg in call.args:
        if isinstance(arg, ast.Call):
            return arg
    for kw in call.keywords:
        if kw.arg == "type_" and isinstance(kw.value, ast.Call):
            return kw.value
    return None


def _check_boolean_column(col_call: ast.Call) -> Finding | None:
    type_call = _column_type_call(col_call)
    if type_call is None or not _is_sa_type(type_call, "Boolean"):
        return None
    nullable_kw = _kwarg(col_call, "nullable")
    if nullable_kw is None:
        return None
    if not _const_is_false(nullable_kw.value):
        return None
    if _has_kw(col_call, "server_default"):
        return None
    name = _const_str(col_call.args[0]) if col_call.args else "<col>"
    return Finding(
        code="boolean_no_server_default",
        message=(
            f"sa.Boolean column '{name}' is NOT NULL with no server_default — "
            "Postgres ALTER TABLE ADD COLUMN against a populated table will fail."
        ),
        lineno=col_call.lineno,
    )


def _check_alter_column_server_default_none(call: ast.Call) -> Finding | None:
    if not _is_call_to(call, owner="op", name="alter_column"):
        return None
    sd = _kwarg(call, "server_default")
    if sd is None or not _const_is_none(sd.value):
        return None
    return Finding(
        code="alter_column_server_default_none_sqlite",
        message=(
            "op.alter_column(..., server_default=None) is not valid SQLite syntax. "
            "Wrap in a dialect guard or split into a Postgres-only branch."
        ),
        lineno=call.lineno,
    )


def _check_op_execute_dialect_sql(call: ast.Call) -> list[Finding]:
    if not _is_call_to(call, owner="op", name="execute"):
        return []
    sql = None
    if call.args:
        sql = _const_str(call.args[0])
    if sql is None:
        return []
    findings: list[Finding] = []
    if _PRAGMA_RE.search(sql):
        findings.append(
            Finding(
                code="op_execute_pragma_sqlite_only",
                message="op.execute SQL contains PRAGMA, which is SQLite-only.",
                lineno=call.lineno,
            )
        )
    if _IF_NOT_EXISTS_RE.search(sql) and not _CREATE_TABLE_RE.search(sql):
        findings.append(
            Finding(
                code="op_execute_if_not_exists_outside_create_table",
                message=(
                    "op.execute SQL uses IF NOT EXISTS outside CREATE TABLE — "
                    "Postgres rejects this in many DDL contexts (e.g. ALTER)."
                ),
                lineno=call.lineno,
            )
        )
    return findings


def _check_batch_alter_table(call: ast.Call) -> Finding | None:
    if not _is_call_to(call, owner="op", name="batch_alter_table"):
        return None
    return Finding(
        code="sqlite_only_pattern_batch_alter_table",
        message=(
            "op.batch_alter_table is required for SQLite but redundant on Postgres. "
            "Recorded for prompt 24 — not a defect."
        ),
        lineno=call.lineno,
    )


def _check_create_foreign_key(call: ast.Call, source: str) -> Finding | None:
    if not _is_call_to(call, owner="op", name="create_foreign_key"):
        return None
    if _NOT_VALID_RE.search(source):
        return None
    return Finding(
        code="fk_addition_no_not_valid",
        message=(
            "op.create_foreign_key without a follow-up ALTER TABLE ... NOT VALID "
            "can hold ACCESS EXCLUSIVE on large Postgres tables."
        ),
        lineno=call.lineno,
    )


def _extract_revision_id(tree: ast.Module, fallback: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "revision":
                    val = _const_str(node.value)
                    if val:
                        return val
    return fallback


def audit_one_file(path: str, *, repo_root: str | None = None) -> RevisionAudit:
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)

    rel = path
    if repo_root is not None:
        try:
            rel = os.path.relpath(path, repo_root)
        except ValueError:
            rel = path
    rel = rel.replace(os.sep, "/")

    base_no_ext = os.path.splitext(os.path.basename(path))[0]
    fallback_rev = base_no_ext.split("_")[0] + "_" + base_no_ext.split("_")[1] if "_" in base_no_ext else base_no_ext
    revision_id = _extract_revision_id(tree, fallback_rev)

    audit = RevisionAudit(revision=revision_id, file=rel)

    for call in _walk_calls(tree):
        f = _check_alter_column_server_default_none(call)
        if f is not None:
            audit.errors.append(f)
            audit.sqlite_compatible = False
            continue

        f = _check_batch_alter_table(call)
        if f is not None:
            audit.warnings.append(f)
            continue

        for ef in _check_op_execute_dialect_sql(call):
            if ef.code == "op_execute_pragma_sqlite_only":
                audit.errors.append(ef)
                audit.postgres_compatible = False
            elif ef.code == "op_execute_if_not_exists_outside_create_table":
                audit.errors.append(ef)
                audit.postgres_compatible = False

        f = _check_create_foreign_key(call, source)
        if f is not None:
            audit.warnings.append(f)
            continue

        if _is_sa_column(call):
            bf = _check_boolean_column(call)
            if bf is not None:
                audit.warnings.append(bf)

    audit.warnings.sort(key=lambda x: (x.lineno, x.code))
    audit.errors.sort(key=lambda x: (x.lineno, x.code))
    return audit


def audit_migrations(versions_dir: str, *, repo_root: str | None = None) -> list[RevisionAudit]:
    """Audit every ``*.py`` revision under ``versions_dir`` (sorted)."""
    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(versions_dir)))
    files = []
    for name in sorted(os.listdir(versions_dir)):
        if not name.endswith(".py") or name.startswith("__"):
            continue
        files.append(os.path.join(versions_dir, name))
    return [audit_one_file(f, repo_root=repo_root) for f in files]


def _git_authored_iso(repo_root: str) -> str:
    """Return the latest commit's authored timestamp; never wall-clock."""
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_root, "log", "-1", "--format=%aI"],
            stderr=subprocess.DEVNULL,
        )
        ts = out.decode("utf-8").strip()
        if ts:
            return ts
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "1970-01-01T00:00:00+00:00"


def build_registry(
    audits: list[RevisionAudit],
    *,
    audited_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "audited_at": audited_at,
        "revisions": [a.to_dict() for a in audits],
    }


def _format_table(audits: list[RevisionAudit]) -> str:
    rows = [
        ("REVISION", "PG", "SQLITE", "WARN", "ERR", "FILE"),
    ]
    for a in audits:
        rows.append(
            (
                a.revision,
                "y" if a.postgres_compatible else "n",
                "y" if a.sqlite_compatible else "n",
                str(len(a.warnings)),
                str(len(a.errors)),
                a.file,
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for idx, r in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
        if idx == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(widths))))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_migrations_postgres_parity",
        description="Audit Alembic migrations for Postgres + SQLite parity.",
    )
    parser.add_argument(
        "--versions-dir",
        default=None,
        help="Path to alembic/versions (default: <repo>/alembic/versions).",
    )
    parser.add_argument(
        "--write-registry",
        action="store_true",
        help="Overwrite data/governed/migration_registry.json with the audit.",
    )
    parser.add_argument(
        "--registry-path",
        default=None,
        help="Override the registry output path (default: data/governed/migration_registry.json).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the registry JSON to stdout instead of the table.",
    )
    parser.add_argument(
        "--audited-at",
        default=None,
        help="Pin the audited_at timestamp (default: latest git authored ISO).",
    )
    args = parser.parse_args(argv)

    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    versions_dir = args.versions_dir or os.path.join(repo_root, "alembic", "versions")
    if not os.path.isdir(versions_dir):
        print(f"error: versions dir not found: {versions_dir}", file=sys.stderr)
        return 2

    audited_at = args.audited_at or _git_authored_iso(repo_root)
    audits = audit_migrations(versions_dir, repo_root=repo_root)
    registry = build_registry(audits, audited_at=audited_at)

    if args.json:
        print(json.dumps(registry, indent=2, sort_keys=False))
    else:
        print(_format_table(audits))
        for a in audits:
            if a.errors or a.warnings:
                print(f"\n{a.revision}  {a.file}")
                for e in a.errors:
                    print(f"  ERROR  L{e.lineno}  {e.code}: {e.message}")
                for w in a.warnings:
                    print(f"  WARN   L{w.lineno}  {w.code}: {w.message}")

    if args.write_registry:
        out_path = args.registry_path or os.path.join(
            repo_root, "data", "governed", "migration_registry.json"
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, sort_keys=False)
            fh.write("\n")

    has_errors = any(a.errors for a in audits)
    return 2 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
