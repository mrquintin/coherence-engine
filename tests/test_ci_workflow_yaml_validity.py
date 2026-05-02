"""Validate CI workflow YAML (Wave 17, prompt 65 of 70).

Parses every workflow file under ``.github/workflows/`` and asserts:

* the file is well-formed YAML and contains both ``jobs`` and a trigger
  declaration (``on``);
* the consolidated ``ci.yml`` exposes the required-checks subset
  (``lint``, ``type``, ``unit_backend``, ``build_backend``,
  ``release_readiness``) plus the gated ``integration_backend`` and
  ``e2e_founder_portal`` jobs;
* the CodeQL config covers both ``python`` and ``javascript``;
* the Dependabot config declares ``package-ecosystem`` entries for
  ``pip``, ``npm``, and ``github-actions``.

This is a static configuration test — it never invokes ``act`` or
otherwise executes a workflow.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_YAML = WORKFLOWS_DIR / "ci.yml"
CODEQL_CONFIG = REPO_ROOT / ".github" / "codeql.yml"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"

REQUIRED_CI_JOBS = {
    "lint",
    "type",
    "unit_backend",
    "integration_backend",
    "unit_frontend",
    "e2e_founder_portal",
    "build_backend",
    "build_apps",
    "release_readiness",
}

REQUIRED_APP_MATRIX = {
    "founder_portal",
    "partner_dashboard",
    "lp_portal",
    "site",
}


yaml = pytest.importorskip("yaml")


def _load_yaml(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _iter_workflow_files():
    assert WORKFLOWS_DIR.is_dir(), f"missing workflows dir: {WORKFLOWS_DIR}"
    for entry in sorted(os.listdir(WORKFLOWS_DIR)):
        if entry.endswith((".yml", ".yaml")):
            yield WORKFLOWS_DIR / entry


def test_every_workflow_parses_and_has_jobs_and_triggers():
    files = list(_iter_workflow_files())
    assert files, "expected at least one workflow file under .github/workflows/"
    for path in files:
        data = _load_yaml(path)
        assert isinstance(data, dict), f"{path.name}: top-level must be a mapping"
        # PyYAML parses the bareword key ``on`` as the boolean ``True`` —
        # accept either spelling.
        trigger = data.get("on", data.get(True))
        assert trigger, f"{path.name}: missing ``on:`` trigger declaration"
        jobs = data.get("jobs")
        assert isinstance(jobs, dict) and jobs, f"{path.name}: missing ``jobs:`` mapping"


def test_ci_yml_declares_required_jobs():
    assert CI_YAML.is_file(), f"missing {CI_YAML}"
    data = _load_yaml(CI_YAML)
    jobs = set(data.get("jobs", {}).keys())
    missing = REQUIRED_CI_JOBS - jobs
    assert not missing, f"ci.yml is missing required jobs: {sorted(missing)}"


def test_ci_yml_has_path_filters_for_server_and_apps():
    data = _load_yaml(CI_YAML)
    changes_job = data["jobs"]["changes"]
    # Inline filters are passed via the action's ``with.filters`` field as a
    # multi-line YAML string. Look for both top-level filter keys.
    rendered = yaml.safe_dump(changes_job)
    assert "server:" in rendered, "ci.yml `changes` job is missing the ``server`` filter"
    assert "apps:" in rendered, "ci.yml `changes` job is missing the ``apps`` filter"
    assert "lp_portal:" in rendered, "ci.yml `changes` job is missing the ``lp_portal`` filter"


def test_ci_yml_frontend_matrices_include_all_apps():
    data = _load_yaml(CI_YAML)
    for job_name in ("unit_frontend", "build_apps", "deploy_preview"):
        matrix = data["jobs"][job_name]["strategy"]["matrix"]
        apps = set(matrix.get("app", []))
        assert REQUIRED_APP_MATRIX <= apps, (
            f"{job_name} matrix missing apps: {sorted(REQUIRED_APP_MATRIX - apps)}"
        )


def test_ci_yml_required_checks_are_not_soft_failed():
    """Required checks must fail the workflow when their command fails."""
    text = CI_YAML.read_text(encoding="utf-8")
    assert "|| true" not in text, "ci.yml must not silence command failures with `|| true`"
    assert "continue-on-error" not in text, "ci.yml must not use continue-on-error"


def test_ci_yml_type_gate_uses_current_typed_service_slice():
    """The backend type gate should be strict, but scoped to typed modules."""
    text = CI_YAML.read_text(encoding="utf-8")
    assert "mypy --strict --follow-imports=skip" in text
    for path in (
        "server/fund/services/policy_parameter_proposals.py",
        "server/fund/services/reserve_optimizer.py",
        "server/fund/services/governed_historical_dataset.py",
        "server/fund/services/calibration_export.py",
    ):
        assert path in text


def test_codeql_config_covers_python_and_javascript():
    assert CODEQL_CONFIG.is_file(), f"missing {CODEQL_CONFIG}"
    data = _load_yaml(CODEQL_CONFIG)
    languages = {lang.lower() for lang in data.get("languages", [])}
    assert "python" in languages, "codeql.yml must include python"
    assert "javascript" in languages, "codeql.yml must include javascript"


def test_dependabot_config_declares_pip_npm_and_actions_ecosystems():
    assert DEPENDABOT_CONFIG.is_file(), f"missing {DEPENDABOT_CONFIG}"
    data = _load_yaml(DEPENDABOT_CONFIG)
    updates = data.get("updates", [])
    ecosystems = {entry.get("package-ecosystem") for entry in updates}
    for required in ("pip", "npm", "github-actions"):
        assert required in ecosystems, (
            f"dependabot.yml missing package-ecosystem={required!r}"
        )


def test_ci_yml_release_readiness_job_runs_the_check_script():
    """The ``release_readiness`` job must invoke release_readiness_check.py."""
    text = CI_YAML.read_text(encoding="utf-8")
    assert "release_readiness_check.py" in text, (
        "ci.yml release_readiness job must call deploy/scripts/release_readiness_check.py"
    )


def test_ci_yml_does_not_cache_secrets_in_artifacts():
    """Sanity guard: artifact paths must not name secret-bearing files."""
    text = CI_YAML.read_text(encoding="utf-8")
    forbidden = (".env\n", "credentials.json", "secrets.yaml", "secrets.yml")
    for needle in forbidden:
        assert needle not in text, f"ci.yml must not reference {needle!r} in artifacts"
