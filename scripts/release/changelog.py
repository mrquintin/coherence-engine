"""Conventional-commit changelog generator (prompt 67).

Parses commit subjects of the form ``type(scope)?: subject`` (with optional
``!`` to indicate a breaking change) and groups them under canonical type
headings. The output is deterministic for a given input list, which is what
``scripts/release/cut_release.py`` and ``tests/test_release_pipeline.py``
rely on.

The parser is intentionally:
  * network-free,
  * git-free (commits can be supplied as a list of strings; a thin
    ``commits_from_git`` helper exists but is only invoked from the CLI
    driver),
  * deterministic (sort key is stable type order then subject).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# Canonical conventional-commit types in display order. Anything that does
# not match goes under "Other" so we never silently drop a commit.
TYPE_ORDER: tuple[str, ...] = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "docs",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)

TYPE_HEADINGS: dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactors",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build",
    "ci": "Continuous Integration",
    "chore": "Chores",
    "revert": "Reverts",
}

OTHER_HEADING = "Other"
BREAKING_HEADING = "BREAKING CHANGES"

# type(scope)!: subject  OR  type: subject
_HEADER_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<bang>!)?"
    r":\s+(?P<subject>.+?)\s*$"
)


@dataclass(frozen=True)
class ParsedCommit:
    sha: str
    type: str
    scope: str | None
    breaking: bool
    subject: str
    body: str = ""

    @property
    def display_type(self) -> str:
        return self.type if self.type in TYPE_HEADINGS else "other"


@dataclass
class Changelog:
    version: str | None = None
    sections: dict[str, list[ParsedCommit]] = field(default_factory=dict)
    breaking: list[ParsedCommit] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.sections and not self.breaking


def parse_commit(sha: str, header: str, body: str = "") -> ParsedCommit | None:
    """Parse a single commit subject. Returns ``None`` on no match."""
    m = _HEADER_RE.match(header.strip())
    if not m:
        return None
    breaking = bool(m.group("bang")) or "BREAKING CHANGE" in (body or "")
    return ParsedCommit(
        sha=sha,
        type=m.group("type").lower(),
        scope=m.group("scope"),
        breaking=breaking,
        subject=m.group("subject").strip(),
        body=(body or "").strip(),
    )


def parse_commits(raw: Iterable[tuple[str, str, str]]) -> list[ParsedCommit]:
    """Parse an iterable of (sha, header, body) tuples.

    Lines that don't match the conventional-commit pattern are mapped to a
    ``ParsedCommit`` with ``type='chore'`` and the original header preserved
    as the subject — that way we never lose a commit, but free-form merges
    don't elevate to a feature/fix bullet.
    """
    out: list[ParsedCommit] = []
    for sha, header, body in raw:
        parsed = parse_commit(sha, header, body)
        if parsed is None:
            out.append(
                ParsedCommit(
                    sha=sha,
                    type="chore",
                    scope=None,
                    breaking=False,
                    subject=header.strip(),
                    body=(body or "").strip(),
                )
            )
        else:
            out.append(parsed)
    return out


def group_commits(commits: Sequence[ParsedCommit]) -> Changelog:
    cl = Changelog()
    for c in commits:
        bucket = cl.sections.setdefault(c.display_type, [])
        bucket.append(c)
        if c.breaking:
            cl.breaking.append(c)
    # Stable, deterministic order within each section: by subject then sha.
    for items in cl.sections.values():
        items.sort(key=lambda x: (x.subject.lower(), x.sha))
    cl.breaking.sort(key=lambda x: (x.subject.lower(), x.sha))
    return cl


def render_markdown(cl: Changelog, *, version: str | None = None) -> str:
    title = f"## {version}" if version else "## Unreleased"
    lines: list[str] = [title, ""]

    if cl.breaking:
        lines.append(f"### {BREAKING_HEADING}")
        lines.append("")
        for c in cl.breaking:
            scope = f"**{c.scope}:** " if c.scope else ""
            lines.append(f"- {scope}{c.subject} ({c.sha[:7]})")
        lines.append("")

    for type_key in TYPE_ORDER:
        items = cl.sections.get(type_key)
        if not items:
            continue
        lines.append(f"### {TYPE_HEADINGS[type_key]}")
        lines.append("")
        for c in items:
            scope = f"**{c.scope}:** " if c.scope else ""
            lines.append(f"- {scope}{c.subject} ({c.sha[:7]})")
        lines.append("")

    if cl.sections.get("other"):
        lines.append(f"### {OTHER_HEADING}")
        lines.append("")
        for c in cl.sections["other"]:
            lines.append(f"- {c.subject} ({c.sha[:7]})")
        lines.append("")

    if cl.is_empty():
        lines.append("_No notable changes._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def commits_from_git(since: str | None, until: str = "HEAD") -> list[tuple[str, str, str]]:
    """Read commit subjects from ``git log``. Used by the CLI; tests pass
    fixture data instead so the unit tests stay deterministic and offline."""
    rev_range = f"{since}..{until}" if since else until
    sep = "\x1e"  # record separator
    fld = "\x1f"  # unit separator
    fmt = f"%H{fld}%s{fld}%b{sep}"
    proc = subprocess.run(
        ["git", "log", rev_range, f"--pretty=format:{fmt}"],
        check=True,
        capture_output=True,
        text=True,
    )
    out: list[tuple[str, str, str]] = []
    for record in proc.stdout.split(sep):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(fld)
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        header = parts[1].strip()
        body = parts[2].strip() if len(parts) >= 3 else ""
        out.append((sha, header, body))
    return out


def commits_from_fixture(path: Path) -> list[tuple[str, str, str]]:
    """Load commits from a JSON fixture: list of {sha, header, body}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str]] = []
    for row in data:
        out.append((row["sha"], row["header"], row.get("body", "")))
    return out


def build_changelog(
    raw_commits: Iterable[tuple[str, str, str]],
    *,
    version: str | None = None,
) -> tuple[Changelog, str]:
    parsed = parse_commits(raw_commits)
    grouped = group_commits(parsed)
    grouped.version = version
    return grouped, render_markdown(grouped, version=version)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", help="Previous tag (inclusive lower bound).")
    p.add_argument("--until", default="HEAD")
    p.add_argument("--version", help="Version label for the section heading.")
    p.add_argument("--fixture", type=Path, help="Read commits from JSON fixture.")
    p.add_argument("--out", type=Path, help="Write rendered Markdown here.")
    p.add_argument("--json-out", type=Path, help="Write structured JSON here.")
    args = p.parse_args(argv)

    if args.fixture:
        raw = commits_from_fixture(args.fixture)
    else:
        raw = commits_from_git(args.since, args.until)

    grouped, md = build_changelog(raw, version=args.version)
    if args.out:
        args.out.write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)

    if args.json_out:
        payload = {
            "version": grouped.version,
            "breaking": [c.__dict__ for c in grouped.breaking],
            "sections": {
                k: [c.__dict__ for c in v] for k, v in grouped.sections.items()
            },
        }
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
