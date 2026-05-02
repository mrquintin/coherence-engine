"""Cut a release: bump VERSION, render CHANGELOG section, summarize artifacts.

Driver script for the tag-triggered release pipeline (prompt 67).

The default invocation is non-mutating; pass ``--write`` to actually edit
``CHANGELOG.md`` and the ``VERSION`` file. ``--dry-run`` is the explicit
read-only mode used by tests and the CI preview job.

Conventional commits are parsed by ``scripts.release.changelog``; the
release-readiness gate is delegated to ``deploy/scripts/release_readiness_check.py``
when ``--require-readiness`` is set (the GitHub Actions release workflow
sets this). The script is intentionally network-free: it never pushes
tags, never publishes containers, never calls ``cosign``. The workflow
does that after ``cut_release.py`` emits the changelog.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent  # coherence_engine/

# Make scripts.release importable regardless of CWD.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.release import changelog as cl_module

VERSION_FILE = _REPO_ROOT / "VERSION"
CHANGELOG_FILE = _REPO_ROOT / "CHANGELOG.md"
CHANGELOG_RELEASE_FILE = _REPO_ROOT / "CHANGELOG_RELEASE.md"
READINESS_SCRIPT = _REPO_ROOT / "deploy" / "scripts" / "release_readiness_check.py"

_SEMVER_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> "SemVer":
        s = raw.strip().lstrip("v")
        m = _SEMVER_RE.match(s)
        if not m:
            raise ValueError(f"Not a semver string: {raw!r}")
        return cls(int(m["major"]), int(m["minor"]), int(m["patch"]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump(self, kind: str) -> "SemVer":
        if kind == "major":
            return SemVer(self.major + 1, 0, 0)
        if kind == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if kind == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        raise ValueError(f"Unknown bump kind: {kind!r}")


def read_version(path: Path = VERSION_FILE) -> SemVer:
    raw = path.read_text(encoding="utf-8").strip()
    return SemVer.parse(raw)


def infer_bump(grouped: cl_module.Changelog) -> str:
    """Decide the next bump from a grouped changelog.

    * Any ``BREAKING CHANGE`` → major.
    * Any ``feat`` → minor.
    * Otherwise → patch.
    """
    if grouped.breaking:
        return "major"
    if grouped.sections.get("feat"):
        return "minor"
    return "patch"


def run_readiness_check() -> tuple[int, str]:
    """Invoke the release-readiness checklist. Returns (exit_code, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(READINESS_SCRIPT), "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(_REPO_ROOT),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def prepend_changelog(existing: str, new_section: str) -> str:
    """Prepend a new release section to ``CHANGELOG.md``, preserving any
    existing ``# Changelog`` header at the top.
    """
    header = "# Changelog\n\n"
    body = existing
    if existing.startswith("# Changelog"):
        # strip the existing header so we don't duplicate it
        first_blank = existing.find("\n\n")
        body = existing[first_blank + 2 :] if first_blank != -1 else ""
    return header + new_section.rstrip() + "\n\n" + body.lstrip()


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", help="Previous release tag (e.g. v0.1.7).")
    p.add_argument("--until", default="HEAD")
    p.add_argument("--fixture", type=Path, help="Use a JSON commit fixture instead of git.")
    p.add_argument(
        "--bump",
        choices=("auto", "major", "minor", "patch"),
        default="auto",
        help="Override the inferred bump.",
    )
    p.add_argument(
        "--require-readiness",
        action="store_true",
        help="Abort if deploy/scripts/release_readiness_check.py fails.",
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan; do not write.")
    p.add_argument("--write", action="store_true", help="Write VERSION + CHANGELOG.md.")
    p.add_argument("--json-out", type=Path, help="Write a machine-readable plan summary.")
    p.add_argument("--release-notes-out", type=Path, help="Write per-release notes for gh release create.")
    args = p.parse_args(argv)

    if args.write and args.dry_run:
        print("--write and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    current = read_version()
    if args.fixture:
        raw = cl_module.commits_from_fixture(args.fixture)
    else:
        raw = cl_module.commits_from_git(args.since, args.until)

    grouped, _preview = cl_module.build_changelog(raw, version=str(current))
    bump_kind = args.bump if args.bump != "auto" else infer_bump(grouped)
    next_version = current.bump(bump_kind)

    # Re-render with the final version label.
    grouped.version = str(next_version)
    rendered = cl_module.render_markdown(grouped, version=str(next_version))

    plan: dict[str, object] = {
        "current_version": str(current),
        "next_version": str(next_version),
        "bump": bump_kind,
        "commit_count": sum(len(v) for v in grouped.sections.values()),
        "breaking_count": len(grouped.breaking),
        "since": args.since,
        "until": args.until,
        "fixture": str(args.fixture) if args.fixture else None,
        "wrote_version_file": False,
        "wrote_changelog_file": False,
        "wrote_release_notes_file": False,
        "readiness_passed": None,
    }

    if args.require_readiness:
        rc, log = run_readiness_check()
        plan["readiness_passed"] = rc == 0
        if rc != 0:
            sys.stderr.write(log)
            print(
                "release-readiness check FAILED — refusing to publish.",
                file=sys.stderr,
            )
            if args.json_out:
                args.json_out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            return 1

    if args.release_notes_out:
        # Release notes are an output artifact (consumed by `gh release create`)
        # and are always written when requested. Dry-run only governs the
        # destructive mutations (VERSION, CHANGELOG.md).
        args.release_notes_out.parent.mkdir(parents=True, exist_ok=True)
        args.release_notes_out.write_text(rendered, encoding="utf-8")
        plan["wrote_release_notes_file"] = True

    if args.write:
        VERSION_FILE.write_text(f"{next_version}\n", encoding="utf-8")
        plan["wrote_version_file"] = True
        existing = CHANGELOG_FILE.read_text(encoding="utf-8") if CHANGELOG_FILE.exists() else ""
        CHANGELOG_FILE.write_text(prepend_changelog(existing, rendered), encoding="utf-8")
        plan["wrote_changelog_file"] = True
    elif args.dry_run:
        print(f"[dry-run] current={current} next={next_version} bump={bump_kind}")
        print()
        sys.stdout.write(rendered)
    else:
        sys.stdout.write(rendered)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
