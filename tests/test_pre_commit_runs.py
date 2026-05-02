"""Smoke test: ``.pre-commit-config.yaml`` is valid and hooks dispatch.

The full ``pre-commit run --all-files`` invocation is expensive and
network-bound (it provisions ruff, mypy, eslint, dotenv-linter, etc.),
so we only attempt it when ``pre-commit`` is already on ``PATH``. When
unavailable we still validate the config statically so CI surfaces
malformed YAML / missing fields without forcing every contributor to
install pre-commit just to run the test suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"


def _load_config() -> dict[str, object]:
    assert CONFIG_PATH.exists(), f"missing {CONFIG_PATH}"
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), "pre-commit config must be a YAML mapping"
    return data


def test_pre_commit_config_is_well_formed() -> None:
    """The config parses, has repos, and includes the required hooks."""
    config = _load_config()

    repos = config.get("repos")
    assert isinstance(repos, list) and repos, "config must define a non-empty 'repos' list"

    seen_hook_ids: set[str] = set()
    for repo in repos:
        assert isinstance(repo, dict), "each repo entry must be a mapping"
        assert "repo" in repo, f"repo entry missing 'repo': {repo!r}"
        hooks = repo.get("hooks") or []
        assert isinstance(hooks, list), f"hooks must be a list: {repo!r}"
        for hook in hooks:
            assert isinstance(hook, dict), "each hook must be a mapping"
            hook_id = hook.get("id")
            assert isinstance(hook_id, str) and hook_id, f"hook missing id: {hook!r}"
            seen_hook_ids.add(hook_id)

    required = {
        "trailing-whitespace",
        "end-of-file-fixer",
        "check-yaml",
        "check-json",
        "check-toml",
        "check-added-large-files",
        "no-commit-to-branch",
        "ruff",
        "ruff-format",
        "mypy",
        "dotenv-linter",
    }
    missing = required - seen_hook_ids
    assert not missing, f"required pre-commit hooks missing: {sorted(missing)}"


def test_pyproject_has_ruff_and_mypy_strict() -> None:
    """`pyproject.toml` carries the ruff and strict-mypy configuration."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in pyproject, "missing [tool.ruff] section"
    assert "[tool.mypy]" in pyproject, "missing [tool.mypy] section"
    assert "strict = true" in pyproject, "mypy must be strict"


@pytest.mark.skipif(
    shutil.which("pre-commit") is None,
    reason="pre-commit is not installed on PATH; skipping live invocation",
)
def test_pre_commit_validate_config_succeeds() -> None:
    """If pre-commit is installed, ``validate-config`` must succeed."""
    result = subprocess.run(
        ["pre-commit", "validate-config", str(CONFIG_PATH)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"pre-commit validate-config failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


@pytest.mark.skipif(
    shutil.which("pre-commit") is None,
    reason="pre-commit is not installed on PATH; skipping live invocation",
)
def test_pre_commit_run_all_files() -> None:
    """Run ``pre-commit run --all-files`` end-to-end when opted in.

    Disabled by default — gated behind ``RUN_PRE_COMMIT_SMOKE=1`` because
    the hook environment provisioning takes minutes and pulls remote
    images on first run. CI lint jobs invoke pre-commit directly.
    """
    if os.environ.get("RUN_PRE_COMMIT_SMOKE") != "1":
        warnings.warn(
            "pre-commit live run skipped (set RUN_PRE_COMMIT_SMOKE=1 to enable)",
            stacklevel=2,
        )
        pytest.skip("opt-in via RUN_PRE_COMMIT_SMOKE=1")

    result = subprocess.run(
        ["pre-commit", "run", "--all-files", "--show-diff-on-failure"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, (
        f"pre-commit run --all-files failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
