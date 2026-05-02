"""Tests for the Cosine Paradox replication harness (prompt 47, Wave 13).

Determinism contract:
  * Re-running the dry-run config on the bundled fixture yields
    byte-identical canonical output (after stripping the volatile
    ``generated_with`` block, which records detected library versions).
  * The pinned ``expected_report.json`` matches the harness output to
    1e-6 on every numeric field.
  * Bumping the seed changes the bootstrap CI but never the
    descriptive stats nor the deterministic Mann-Whitney U statistic.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from coherence_engine.Experiments.Cosine_Paradox_Replication import (
    run_replication as cp,
)


HERE = Path(cp.__file__).resolve().parent
FIXTURE_PATH = HERE / "fixtures" / "tiny_nli_fixture.json"
EXPECTED_PATH = HERE / "expected_report.json"
PREREG_PATH = HERE / "preregistration.yaml"


def _dry_run_config(**overrides):
    base = dict(
        seed=47,
        n_permutations=1000,
        n_bootstrap_iterations=1000,
        alpha=0.01,
        fixture_path=str(FIXTURE_PATH),
        minimum_n_per_label_override=4,
    )
    base.update(overrides)
    return cp.ReplicationConfig(**base)


def _strip_volatile(d):
    """Remove the ``generated_with`` block that records library versions."""
    out = dict(d)
    out.pop("generated_with", None)
    return out


def _compare_numeric(a, b, *, tol=1e-6, path="$"):
    """Walk two structures and assert every numeric is within ``tol``."""
    if isinstance(a, dict):
        assert isinstance(b, dict), f"{path}: type mismatch dict vs {type(b).__name__}"
        assert set(a.keys()) == set(b.keys()), (
            f"{path}: key-set mismatch:\n  expected: {sorted(b.keys())}\n  actual:   {sorted(a.keys())}"
        )
        for k in a:
            _compare_numeric(a[k], b[k], tol=tol, path=f"{path}.{k}")
        return
    if isinstance(a, list):
        assert isinstance(b, list), f"{path}: type mismatch list vs {type(b).__name__}"
        assert len(a) == len(b), f"{path}: length mismatch {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _compare_numeric(x, y, tol=tol, path=f"{path}[{i}]")
        return
    if isinstance(a, float) or isinstance(b, float):
        assert math.isfinite(float(a)) == math.isfinite(float(b)), (
            f"{path}: finiteness mismatch {a!r} vs {b!r}"
        )
        if math.isfinite(float(a)):
            assert abs(float(a) - float(b)) <= tol, (
                f"{path}: |{a} - {b}| > {tol}"
            )
        return
    assert a == b, f"{path}: {a!r} != {b!r}"


# ---------------------------------------------------------------------------
# Statistical primitives
# ---------------------------------------------------------------------------


def test_ranks_with_ties_handles_ties():
    ranks = cp._ranks_with_ties([10.0, 20.0, 20.0, 30.0])
    # ranks 1, 2.5, 2.5, 4 (1-based, average for ties)
    assert ranks == [1.0, 2.5, 2.5, 4.0]


def test_mann_whitney_u_perfect_separation():
    a = [1.0, 2.0, 3.0]
    b = [10.0, 11.0, 12.0]
    U_a, U_b = cp.mann_whitney_u(a, b)
    # All a values are smaller -> U_a = 0, U_b = n1*n2 = 9
    assert U_a == 0.0
    assert U_b == 9.0


def test_rank_biserial_effect_size_bounds():
    # U_a = 0  -> r = -1  (group A entirely below B)
    assert cp.rank_biserial_effect_size(0.0, 5, 5) == -1.0
    # U_a = n1*n2 -> r = +1
    assert cp.rank_biserial_effect_size(25.0, 5, 5) == 1.0
    # U_a = n1*n2/2 -> r = 0
    assert cp.rank_biserial_effect_size(12.5, 5, 5) == 0.0


def test_descriptive_stats_basic():
    stats = cp.descriptive_stats([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["n"] == 5
    assert stats["mean"] == pytest.approx(3.0, abs=1e-9)
    assert stats["median"] == pytest.approx(3.0, abs=1e-9)
    assert stats["min"] == 1.0
    assert stats["max"] == 5.0


def test_permutation_test_separable_groups_low_p():
    a = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    b = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    p = cp.permutation_test_u(a, b, iterations=500, seed=1)
    assert p < 0.05


def test_permutation_test_overlapping_groups_high_p():
    a = [0.1, 0.5, 0.3, 0.2, 0.4]
    b = [0.4, 0.2, 0.5, 0.1, 0.3]
    p = cp.permutation_test_u(a, b, iterations=500, seed=1)
    assert p > 0.5


# ---------------------------------------------------------------------------
# Pre-registration parsing
# ---------------------------------------------------------------------------


def test_preregistration_loads_and_has_required_keys():
    prereg = cp.load_preregistration()
    assert prereg["version"] == "v1.0"
    assert prereg["random_seed"] == 47
    assert prereg["primary_hypothesis"]["alpha"] == 0.01
    assert prereg["primary_hypothesis"]["n_permutations"] == 10000
    assert prereg["bootstrap"]["iterations"] == 10000
    assert prereg["stopping_rule"]["minimum_n_per_label"] == 200


# ---------------------------------------------------------------------------
# End-to-end harness
# ---------------------------------------------------------------------------


def test_dry_run_matches_pinned_expected_report():
    config = _dry_run_config()
    report = cp.run_replication(config)
    actual = report.to_canonical_dict()
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    _compare_numeric(_strip_volatile(actual), _strip_volatile(expected), tol=1e-6)


def test_dry_run_is_byte_identical_across_runs():
    a = cp.run_replication(_dry_run_config()).to_canonical_bytes()
    b = cp.run_replication(_dry_run_config()).to_canonical_bytes()
    assert a == b, "harness must produce byte-identical canonical output"


def test_falsification_outcome_paradox_confirmed_on_fixture():
    report = cp.run_replication(_dry_run_config())
    falsi = report.falsification
    assert falsi["outcome"] == "paradox_confirmed"
    assert falsi["paradox_refuted"] is False
    # Sanity: small effect, high p
    assert abs(falsi["observed_effect"]) < 0.20
    assert falsi["observed_p_value"] >= 0.01


def test_secondary_tests_separate_neutral_from_entailment_and_contradiction():
    report = cp.run_replication(_dry_run_config())
    secondary = report.secondary_tests
    for key in ("neutral_vs_entailment", "neutral_vs_contradiction"):
        assert secondary[key]["reject_null"] is True
        assert abs(secondary[key]["rank_biserial_effect_size"]) >= 0.95


def test_descriptive_stats_match_fixture_n_per_label():
    report = cp.run_replication(_dry_run_config())
    for label in ("entailment", "contradiction", "neutral"):
        assert report.descriptive[label]["n"] == 8


def test_inputs_digest_changes_when_cosines_change(tmp_path):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    digest_a = cp.run_replication(_dry_run_config()).inputs["cosines_digest_sha256"]

    perturbed = json.loads(json.dumps(fixture))
    perturbed["rows"][0]["cosine"] = 0.9999
    perturbed_path = tmp_path / "perturbed.json"
    perturbed_path.write_text(json.dumps(perturbed), encoding="utf-8")
    config = _dry_run_config(fixture_path=str(perturbed_path))
    digest_b = cp.run_replication(config).inputs["cosines_digest_sha256"]

    assert digest_a != digest_b


def test_changing_seed_changes_ci_but_not_point_estimate():
    # Use a low bootstrap iter count so the percentiles haven't yet
    # converged on the (tiny) fixture; with the production iter count
    # the bootstrap distribution is so well-resolved that two seeds can
    # land on the same discrete percentile.
    a = cp.run_replication(_dry_run_config(
        seed=1, n_permutations=200, n_bootstrap_iterations=200,
    ))
    b = cp.run_replication(_dry_run_config(
        seed=2, n_permutations=200, n_bootstrap_iterations=200,
    ))
    # Point estimates are deterministic functions of the data, not of the seed.
    assert a.primary_test["rank_biserial_effect_size"] == b.primary_test["rank_biserial_effect_size"]
    assert a.primary_test["mann_whitney_u"] == b.primary_test["mann_whitney_u"]
    # CIs depend on seeded bootstrap.
    assert a.primary_test["rank_biserial_ci"] != b.primary_test["rank_biserial_ci"]


def test_insufficient_sample_raises_when_below_minimum(tmp_path):
    payload = {
        "schema": "cosine-paradox-fixture-v1",
        "source": "real",  # not "fixture" -> stopping rule applies
        "rows": [
            {"label": "entailment", "cosine": 0.8},
            {"label": "contradiction", "cosine": 0.85},
            {"label": "neutral", "cosine": 0.4},
        ],
    }
    p = tmp_path / "tiny.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    config = cp.ReplicationConfig(
        seed=47,
        n_permutations=100,
        n_bootstrap_iterations=100,
        cosines_path=str(p),
    )
    with pytest.raises(cp.InsufficientSampleError):
        cp.run_replication(config)


def test_dataset_path_without_allow_network_is_refused():
    config = cp.ReplicationConfig(
        seed=47,
        dataset_path="/tmp/nonexistent.jsonl",
        allow_network=False,
    )
    with pytest.raises(cp.NetworkAccessDenied):
        cp.run_replication(config)


def test_falsification_threshold_pinned_at_0_20():
    # Prevents accidental post-hoc loosening of the criterion.
    assert cp._FALSIFICATION_EFFECT_THRESHOLD == 0.20


# ---------------------------------------------------------------------------
# CLI integration (smoke)
# ---------------------------------------------------------------------------


def test_cli_dry_run_emits_matching_report(tmp_path):
    # Run the CLI module the same way the verification command does.
    repo = Path(cp.__file__).resolve().parents[2]  # /<...>/coherence_engine
    parent = repo.parent
    out_path = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "coherence_engine", "replication",
         "cosine-paradox", "--dry-run", "--output", str(out_path)],
        cwd=parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"CLI failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert out_path.is_file()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == cp.REPLICATION_SCHEMA_VERSION
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    _compare_numeric(_strip_volatile(payload), _strip_volatile(expected), tol=1e-6)
