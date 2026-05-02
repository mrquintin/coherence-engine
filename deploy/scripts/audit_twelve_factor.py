"""Twelve-factor compliance auditor.

Walks the codebase with ``ast`` and flags violations of the rules
documented in ``docs/specs/twelve_factor.md``:

* Direct ``os.environ`` / ``os.getenv`` reads outside of allowed
  config / secret modules.
* Hardcoded ``http(s)://...`` URLs in production source.
* ``print(...)`` calls in non-CLI code (must use ``logging``).
* Outbound ``requests.{get,post,put,...}`` / ``httpx.{...}`` calls
  that omit a ``timeout=`` kwarg (best-effort heuristic).

Output is a deterministic JSON report. Exit code 0 if there are no
``severity: "error"`` findings, 2 otherwise. Designed to run both
locally (``python -m coherence_engine config audit``) and in CI.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, List, Optional


# ── Path policy ──────────────────────────────────────────────

# Files where direct env reads are allowed (the config/secret layer).
ENV_READ_ALLOWLIST = {
    "server/fund/config.py",
    "server/fund/services/secret_manager.py",
    "server/fund/services/secret_backends.py",
    "server/fund/services/secret_manifest.py",
    "deploy/scripts/secret_manager_preflight.py",
    "deploy/scripts/audit_twelve_factor.py",
    "alembic/env.py",
}

# Path prefixes where env reads are operationally legitimate (CI / ops
# scripts that take env-driven inputs from the runner). They are still
# reported, but at WARN severity so they don't block the gate.
ENV_READ_WARN_PREFIXES = (
    "deploy/scripts/",
    "alembic/",
    "cli.py",
)

# CLI / script files where ``print`` is part of the contract.
PRINT_ALLOWED_PREFIXES = (
    "cli.py",
    "deploy/scripts/",
    "scripts/",
    "tests/",
    "alembic/",
)

# Roots that we walk. We deliberately skip data, build, .git, etc.
SOURCE_ROOTS = ("server", "deploy/scripts", "alembic")
SOURCE_FILES_AT_ROOT = ("cli.py",)

# Exclude patterns (substring match against rel_path).
EXCLUDE_SUBSTRINGS = (
    "/__pycache__/",
    "/.git/",
    "/build/",
    "/.pytest_cache/",
    "/tests/",
    "/test_",
    "/fixtures/",
)

# URL allowlist: documentation hosts, schema namespaces, examples.
URL_ALLOWLIST_HOSTS = {
    "schemas.coherence-engine.local",
    "schemas.coherence-engine.io",
    "example.com",
    "localhost",
    "127.0.0.1",
}
URL_REGEX = re.compile(r"https?://([^/\s\"']+)")


# ── Severity ────────────────────────────────────────────────

SEV_ERROR = "error"
SEV_WARN = "warn"


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    col: int
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditReport:
    schema_version: str = "1"
    findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        by_sev: dict = {"error": 0, "warn": 0}
        for f in self.findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        return {
            "schema_version": self.schema_version,
            "summary": {
                "total": len(self.findings),
                "by_severity": by_sev,
            },
            "findings": [f.to_dict() for f in sorted(
                self.findings,
                key=lambda x: (x.severity != "error", x.path, x.line, x.rule),
            )],
        }


# ── AST visitors ────────────────────────────────────────────

class _Visitor(ast.NodeVisitor):
    def __init__(self, rel_path: str, source: str):
        self.rel_path = rel_path
        self.source_lines = source.splitlines()
        self.findings: List[Finding] = []
        self._allow_env = rel_path in ENV_READ_ALLOWLIST
        self._env_severity = (
            SEV_WARN
            if rel_path.startswith(ENV_READ_WARN_PREFIXES)
            else SEV_ERROR
        )
        self._allow_print = rel_path.startswith(PRINT_ALLOWED_PREFIXES)

    # ---- env reads ----
    def visit_Attribute(self, node: ast.Attribute) -> None:
        # os.environ.get(...)  (Attribute("get", value=Attribute("environ", value=Name("os"))))
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
        ):
            if not self._allow_env:
                self.findings.append(Finding(
                    rule="env_read_outside_config",
                    severity=self._env_severity,
                    path=self.rel_path,
                    line=node.lineno,
                    col=node.col_offset,
                    message="os.environ access outside config/secret layer",
                ))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ["KEY"]
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
            and not self._allow_env
        ):
            self.findings.append(Finding(
                rule="env_read_outside_config",
                severity=self._env_severity,
                path=self.rel_path,
                line=node.lineno,
                col=node.col_offset,
                message="os.environ[...] subscript outside config/secret layer",
            ))
        self.generic_visit(node)

    # ---- print + os.getenv + http calls ----
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        # os.getenv(...)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr == "getenv"
            and not self._allow_env
        ):
            self.findings.append(Finding(
                rule="env_read_outside_config",
                severity=self._env_severity,
                path=self.rel_path,
                line=node.lineno,
                col=node.col_offset,
                message="os.getenv outside config/secret layer",
            ))

        # print(...)
        if isinstance(func, ast.Name) and func.id == "print" and not self._allow_print:
            self.findings.append(Finding(
                rule="print_in_runtime_code",
                severity=SEV_WARN,
                path=self.rel_path,
                line=node.lineno,
                col=node.col_offset,
                message="print(...) in runtime code; use the logger instead",
            ))

        # requests.<verb>(...) / httpx.<verb>(...) without timeout=
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            mod = func.value.id
            verb = func.attr
            if mod in ("requests", "httpx") and verb in (
                "get", "post", "put", "patch", "delete", "request", "head", "options",
            ):
                has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                if not has_timeout:
                    self.findings.append(Finding(
                        rule="http_call_without_timeout",
                        severity=SEV_WARN,
                        path=self.rel_path,
                        line=node.lineno,
                        col=node.col_offset,
                        message=f"{mod}.{verb}(...) missing timeout= kwarg",
                    ))

        self.generic_visit(node)

    # ---- hardcoded URLs ----
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            for match in URL_REGEX.finditer(node.value):
                host = match.group(1).split(":")[0]
                if host in URL_ALLOWLIST_HOSTS:
                    continue
                if host.endswith(".local") or host.endswith(".internal"):
                    continue
                self.findings.append(Finding(
                    rule="hardcoded_url",
                    severity=SEV_WARN,
                    path=self.rel_path,
                    line=node.lineno,
                    col=node.col_offset,
                    message=f"hardcoded URL host {host!r}; promote to Settings",
                ))
        self.generic_visit(node)


# ── Walker ──────────────────────────────────────────────────

def _iter_python_files(repo_root: Path) -> Iterable[Path]:
    for rel_top in SOURCE_FILES_AT_ROOT:
        p = repo_root / rel_top
        if p.is_file():
            yield p
    for rel_root in SOURCE_ROOTS:
        base = repo_root / rel_root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
            if any(token in f"/{rel}" for token in EXCLUDE_SUBSTRINGS):
                continue
            yield path


def audit(repo_root: Path) -> AuditReport:
    report = AuditReport()
    for path in _iter_python_files(repo_root):
        rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            report.findings.append(Finding(
                rule="syntax_error",
                severity=SEV_WARN,
                path=rel,
                line=exc.lineno or 0,
                col=exc.offset or 0,
                message=f"could not parse: {exc.msg}",
            ))
            continue
        v = _Visitor(rel, source)
        v.visit(tree)
        report.findings.extend(v.findings)
    return report


# ── Entrypoint ──────────────────────────────────────────────

def _resolve_repo_root(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    # deploy/scripts/audit_twelve_factor.py -> repo root
    return here.parent.parent.parent


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_twelve_factor",
        description="Twelve-factor compliance auditor for the Coherence fund backend.",
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help="Repo root to walk (default: inferred from this script's location).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Where to write the JSON report (default: stdout).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "summary"),
        default="json",
        help="Output format (default: json).",
    )
    args = parser.parse_args(argv)

    root = _resolve_repo_root(args.repo_root)
    report = audit(root)
    payload = report.to_dict()

    body = json.dumps(payload, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(body + "\n", encoding="utf-8")
    elif args.format == "summary":
        s = payload["summary"]
        sys.stdout.write(
            f"twelve-factor audit: total={s['total']} error={s['by_severity'].get('error', 0)} "
            f"warn={s['by_severity'].get('warn', 0)}\n"
        )
    else:
        sys.stdout.write(body + "\n")

    return 0 if payload["summary"]["by_severity"].get("error", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
