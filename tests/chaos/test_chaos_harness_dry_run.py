"""Dry-run tests for the chaos harness (prompt 64 of 70).

These tests exercise the YAML schema validator and the runner's
dry-run path. They never start docker, never hit the network, and
never honor ``CHAOS=1`` (the runner's live path is exercised by hand
when an operator provisions the docker-compose topology — see
``docs/ops/chaos.md``).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# The chaos runner ships under ``deploy/chaos/`` which is not a Python
# package (no ``__init__.py`` — it's an operations directory, not an
# importable module). Load it via ``importlib`` so the tests do not
# require any sys.path manipulation in conftest.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO_ROOT / "deploy" / "chaos" / "run_scenario.py"
_SCENARIOS_DIR = _REPO_ROOT / "deploy" / "chaos" / "scenarios"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "coherence_engine_chaos_run_scenario", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


@pytest.fixture
def all_scenarios():
    return sorted(_SCENARIOS_DIR.glob("*.yaml"))


def test_scenarios_directory_is_populated(all_scenarios):
    # The four shipped scenarios are the contract referenced in the
    # ops doc; if one of them disappears, this test catches it before
    # docs go stale.
    names = {p.name for p in all_scenarios}
    assert names == {
        "db_primary_failover.yaml",
        "redis_flap.yaml",
        "s3_outage.yaml",
        "worker_death_mid_job.yaml",
    }, f"unexpected scenario set: {names}"


def test_every_shipped_scenario_passes_schema_validation(runner, all_scenarios):
    for scenario_path in all_scenarios:
        # load_scenario raises on any structural issue.
        doc = runner.load_scenario(scenario_path)
        assert doc["schema_version"] == "chaos-scenario-v1"
        # The byte-identical artifact replay assertion is the
        # determinism contract. Every scenario must declare it.
        kinds = {pc["kind"] for pc in doc["post_conditions"]}
        assert "byte_identical_artifact_replay" in kinds, (
            f"{scenario_path.name} is missing the determinism contract"
        )


def test_dry_run_exits_zero_on_representative_scenario(runner, tmp_path):
    scenario = _SCENARIOS_DIR / "db_primary_failover.yaml"
    json_out = tmp_path / "report.json"
    rc = runner.run_scenario(scenario, dry_run=True, json_out=json_out)
    assert rc == 0

    report = json.loads(json_out.read_text())
    assert report["scenario"] == "db_primary_failover"
    assert report["mode"] == "dry_run"
    assert report["ok"] is True
    assert report["perturbation_steps"] >= 1
    assert "byte_identical_artifact_replay" in report["post_conditions"]


def test_dry_run_argv_path_exits_zero(runner):
    rc = runner.main(
        [
            "--scenario",
            str(_SCENARIOS_DIR / "redis_flap.yaml"),
            "--dry-run",
        ]
    )
    assert rc == 0


def test_invalid_schema_version_fails_validation(runner, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "\n".join(
            [
                "schema_version: not-a-real-version",
                "name: bad",
                "description: x",
                "slo: {end_to_end_seconds: 30}",
                "pre_state: {required_services: [a], startup_timeout_seconds: 5}",
                "perturbation: [{action: stop, target: a}]",
                "workload: {kind: synthetic_application_submit, count: 1, "
                "application_fixture: x.json, "
                "wait_for_completion_timeout_seconds: 30}",
                "post_conditions: [{kind: byte_identical_artifact_replay}]",
                "",
            ]
        )
    )
    rc = runner.run_scenario(bad, dry_run=True)
    assert rc == 2


def test_missing_byte_identical_replay_post_condition_fails(runner, tmp_path):
    # The determinism contract: every scenario MUST assert byte-identical
    # artifact replay. If a scenario forgets it, validation must fail.
    bad = tmp_path / "no_determinism.yaml"
    bad.write_text(
        "\n".join(
            [
                "schema_version: chaos-scenario-v1",
                "name: no_determinism",
                "description: forgot the determinism contract",
                "slo: {end_to_end_seconds: 30}",
                "pre_state: {required_services: [a], startup_timeout_seconds: 5}",
                "perturbation: [{action: stop, target: a}]",
                "workload: {kind: synthetic_application_submit, count: 1, "
                "application_fixture: x.json, "
                "wait_for_completion_timeout_seconds: 30}",
                "post_conditions: [{kind: idempotency_intact}]",
                "",
            ]
        )
    )
    rc = runner.run_scenario(bad, dry_run=True)
    assert rc == 2


def test_unknown_perturbation_action_is_rejected(runner, tmp_path):
    bad = tmp_path / "bad_action.yaml"
    bad.write_text(
        "\n".join(
            [
                "schema_version: chaos-scenario-v1",
                "name: bad_action",
                "description: invalid action",
                "slo: {end_to_end_seconds: 30}",
                "pre_state: {required_services: [a], startup_timeout_seconds: 5}",
                "perturbation: [{action: explode, target: a}]",
                "workload: {kind: synthetic_application_submit, count: 1, "
                "application_fixture: x.json, "
                "wait_for_completion_timeout_seconds: 30}",
                "post_conditions: [{kind: byte_identical_artifact_replay}]",
                "",
            ]
        )
    )
    rc = runner.run_scenario(bad, dry_run=True)
    assert rc == 2


def test_live_mode_refused_without_chaos_env(runner, monkeypatch):
    # The runner must refuse to apply perturbations unless CHAOS=1 is
    # set. This protects CI / shared dev environments.
    monkeypatch.delenv("CHAOS", raising=False)
    rc = runner.run_scenario(
        _SCENARIOS_DIR / "s3_outage.yaml", dry_run=False
    )
    assert rc == 3


def test_validate_scenario_rejects_non_mapping(runner):
    with pytest.raises(runner.ScenarioError):
        runner.validate_scenario("not a dict")  # type: ignore[arg-type]


def test_validate_scenario_rejects_zero_slo(runner):
    doc = {
        "schema_version": "chaos-scenario-v1",
        "name": "x",
        "description": "x",
        "slo": {"end_to_end_seconds": 0},
        "pre_state": {"required_services": ["a"], "startup_timeout_seconds": 5},
        "perturbation": [{"action": "stop", "target": "a"}],
        "workload": {
            "kind": "synthetic_application_submit",
            "count": 1,
            "application_fixture": "x.json",
            "wait_for_completion_timeout_seconds": 30,
        },
        "post_conditions": [{"kind": "byte_identical_artifact_replay"}],
    }
    with pytest.raises(runner.ScenarioError):
        runner.validate_scenario(doc)
