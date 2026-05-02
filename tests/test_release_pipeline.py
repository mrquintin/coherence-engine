"""Tests for the release pipeline scaffolding (prompt 67).

Two contracts:

1. ``scripts/release/cut_release.py --dry-run --fixture <commits.json>``
   produces a deterministic, conventional-commit grouped Markdown changelog
   for a fixed set of commits. Run twice → identical output.

2. ``deploy/helm/`` renders without error. Skipped when ``helm`` is not on
   ``PATH`` (the umbrella chart structure is still asserted at the file
   system level).

The fixture commit log is committed inline so the test is hermetic and
does not depend on `git log`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CUT_RELEASE = REPO_ROOT / "scripts" / "release" / "cut_release.py"
CHANGELOG_MOD = REPO_ROOT / "scripts" / "release" / "changelog.py"
HELM_CHART_ROOT = REPO_ROOT / "deploy" / "helm"
HELM_SUBCHART = HELM_CHART_ROOT / "coherence-fund"
DOCKERFILE = REPO_ROOT / "Dockerfile"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


# Make scripts.release importable in this test process.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


FIXTURE_COMMITS = [
    {"sha": "aaaaaaa1111111111111111111111111aaaaaaaa",
     "header": "feat(api): add /v1/portfolio overrides endpoint", "body": ""},
    {"sha": "bbbbbbb2222222222222222222222222bbbbbbbb",
     "header": "fix(scoring): clamp transcript turn count >= 1", "body": ""},
    {"sha": "ccccccc3333333333333333333333333cccccccc",
     "header": "docs(ops): add cosign verify recipe", "body": ""},
    {"sha": "ddddddd4444444444444444444444444dddddddd",
     "header": "chore(deps): bump fastapi to 0.115", "body": ""},
    {"sha": "eeeeeee5555555555555555555555555eeeeeeee",
     "header": "feat(workflow)!: rename WorkflowRun.state column",
     "body": "BREAKING CHANGE: column renamed; clients must re-issue."},
    {"sha": "fffffff6666666666666666666666666ffffffff",
     "header": "perf(decision): cache canonical artifact hash", "body": ""},
    {"sha": "9999999777777777777777777777777799999999",
     "header": "rebuild ci infra without prefix", "body": ""},  # not conventional → falls into chore-tagged catch-all
    {"sha": "8888888aaaaaaaaaaaaaaaaaaaaaaaa888888888",
     "header": "test(integration): cover artifact reproducibility", "body": ""},
]


# ---------------------------------------------------------------------------
# 1. Determinism + conventional grouping
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_path(tmp_path: Path) -> Path:
    p = tmp_path / "commits.json"
    p.write_text(json.dumps(FIXTURE_COMMITS), encoding="utf-8")
    return p


def _run_cut_release(fixture: Path, tmp_path: Path) -> tuple[str, dict]:
    notes = tmp_path / "CHANGELOG_RELEASE.md"
    plan = tmp_path / "release-plan.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(CUT_RELEASE),
            "--dry-run",
            "--fixture",
            str(fixture),
            "--release-notes-out",
            str(notes),
            "--json-out",
            str(plan),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[dry-run]" in proc.stdout
    return notes.read_text(encoding="utf-8"), json.loads(plan.read_text(encoding="utf-8"))


def test_cut_release_dry_run_is_deterministic(fixture_path: Path, tmp_path: Path) -> None:
    md_a, plan_a = _run_cut_release(fixture_path, tmp_path / "a")
    md_b, plan_b = _run_cut_release(fixture_path, tmp_path / "b")
    assert md_a == md_b, "changelog rendering must be deterministic across runs"
    # The plan summary should agree on counts and bump.
    assert plan_a["bump"] == plan_b["bump"]
    assert plan_a["next_version"] == plan_b["next_version"]


def test_cut_release_groups_conventional_commits(fixture_path: Path, tmp_path: Path) -> None:
    md, plan = _run_cut_release(fixture_path, tmp_path)
    # Section headings present in display order.
    assert "### BREAKING CHANGES" in md
    assert "### Features" in md
    assert "### Bug Fixes" in md
    assert "### Performance" in md
    assert "### Documentation" in md
    assert "### Tests" in md
    assert "### Chores" in md
    assert "Features" in md.split("BREAKING CHANGES", 1)[1]
    # A breaking commit triggers a major bump.
    assert plan["bump"] == "major"
    assert plan["breaking_count"] == 1
    # Subjects are present, scopes bolded.
    assert "**api:** add /v1/portfolio overrides endpoint" in md
    assert "**scoring:** clamp transcript turn count >= 1" in md
    # Free-form (non-conventional) commit lands in chores, not silently lost.
    assert "rebuild ci infra without prefix" in md


def test_cut_release_writes_release_notes_file(fixture_path: Path, tmp_path: Path) -> None:
    notes = tmp_path / "release-notes.md"
    proc = subprocess.run(
        [
            sys.executable,
            str(CUT_RELEASE),
            "--fixture", str(fixture_path),
            "--release-notes-out", str(notes),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert notes.exists()
    assert notes.read_text(encoding="utf-8").startswith("## ")
    # No --dry-run flag was passed and no --write — stdout still has the rendered changelog.
    assert "## " in proc.stdout


def test_changelog_module_classifies_known_types() -> None:
    from scripts.release import changelog as cl

    parsed = cl.parse_commit("a" * 40, "feat(api): hello")
    assert parsed is not None
    assert parsed.type == "feat"
    assert parsed.scope == "api"
    assert parsed.breaking is False

    breaking = cl.parse_commit("b" * 40, "refactor(core)!: drop Python 3.8")
    assert breaking is not None
    assert breaking.breaking is True

    body_breaking = cl.parse_commit("c" * 40, "fix: x", body="BREAKING CHANGE: y")
    assert body_breaking is not None
    assert body_breaking.breaking is True

    free = cl.parse_commit("d" * 40, "merge branch 'release/1'")
    assert free is None  # caller decides what to do with unparseable headers


def test_changelog_render_orders_sections() -> None:
    from scripts.release import changelog as cl

    parsed = cl.parse_commits([
        ("1" * 40, "fix: b", ""),
        ("2" * 40, "feat: a", ""),
    ])
    grouped = cl.group_commits(parsed)
    md = cl.render_markdown(grouped, version="1.2.3")
    feat_idx = md.index("### Features")
    fix_idx = md.index("### Bug Fixes")
    assert feat_idx < fix_idx  # canonical order


# ---------------------------------------------------------------------------
# 2. Helm chart structure + render
# ---------------------------------------------------------------------------

def test_helm_umbrella_chart_files_present() -> None:
    chart_yaml = HELM_CHART_ROOT / "Chart.yaml"
    values_yaml = HELM_CHART_ROOT / "values.yaml"
    assert chart_yaml.is_file(), "deploy/helm/Chart.yaml is required"
    assert values_yaml.is_file(), "deploy/helm/values.yaml is required"
    text = chart_yaml.read_text(encoding="utf-8")
    assert "apiVersion: v2" in text
    assert "name: coherence-engine" in text
    assert "dependencies:" in text
    # Subchart is reachable as either a directory or a symlink in charts/.
    subchart_link = HELM_CHART_ROOT / "charts" / "coherence-fund"
    assert subchart_link.exists(), "expected deploy/helm/charts/coherence-fund (file:// dep) — run `helm dep build` or symlink"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")
def test_helm_template_renders_without_error() -> None:
    proc = subprocess.run(
        ["helm", "template", str(HELM_CHART_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    # Render produced at least one Kubernetes object.
    assert "kind:" in proc.stdout


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")
def test_helm_subchart_lints_clean() -> None:
    proc = subprocess.run(
        ["helm", "lint", str(HELM_SUBCHART)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"helm lint failed:\n{proc.stdout}\n{proc.stderr}"


# ---------------------------------------------------------------------------
# 3. Workflow + Dockerfile sanity
# ---------------------------------------------------------------------------

def test_release_workflow_has_tag_trigger_and_cosign() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "tags:" in text
    assert "v*.*.*" in text
    assert "cosign" in text
    assert "release_publish" in text
    # Prohibition assertions: bump VERSION PR step exists.
    assert "release/post-" in text
    assert "VERSION" in text


def test_backend_dockerfile_is_multistage_python() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert text.count("FROM python:") >= 2, "must be a multi-stage python image"
    assert "uvicorn" in text
    assert "USER app" in text  # never run as root
