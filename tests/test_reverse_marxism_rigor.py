"""Tests for the reverse-Marxism reflection-recovery rigor harness
(prompt 50, Wave 13).

The harness exists to test whether the headline 84.3% recovery
generalises under stricter conditions. The two contracts here are:

  * **Statistical**: a synthetic fixture in which reflection across
    the fitted axis flips every held-out sentence to the side
    opposite its ideology label (recovery near 1.0 at alpha=2),
    while reflection across random unit vectors leaves the
    projection essentially unchanged (recovery far from the held-
    out value — well below 0.5 — so the contrast carries the test).
  * **Determinism**: canonical report bytes are byte-identical
    across runs given the same config and inputs (after stripping
    the volatile ``generated_with`` block).

Note on the random-baseline empirical mean: the pre-registration
calls 0.5 the *idealised* null. With finite-norm vectors and high
embedding dimension, random unit vectors as reflection axes hardly
perturb the projection on the fitted axis, so empirically the
baseline lands near 0, not near 0.5. The decision rule uses the
empirical baseline CI (not the 0.5 idealisation) — which is exactly
what the rigor study is supposed to do.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from coherence_engine.Experiments.Reverse_Marxism_Rigor import (
    run_rigor_study as rmr,
)


HERE = Path(rmr.__file__).resolve().parent
FIXTURE_PATH = HERE / "fixtures" / "tiny_rigor_fixture.json"
PREREG_PATH = HERE / "preregistration.yaml"


def _dry_run_config(**overrides):
    base = dict(
        seed=50,
        n_random_axes=32,
        n_bootstrap=200,
        ci_percent=95.0,
        alpha_grid=(0.5, 1.0, 1.5, 2.0, 2.5, 3.0),
        fixture_path=str(FIXTURE_PATH),
        minimum_holdout_override=4,
        minimum_axis_seeds_override=2,
    )
    base.update(overrides)
    return rmr.RigorConfig(**base)


def _strip_volatile(d):
    out = dict(d)
    out.pop("generated_with", None)
    return out


# ---------------------------------------------------------------------------
# Reflection math
# ---------------------------------------------------------------------------


class TestHouseholderReflect:
    def test_alpha_2_flips_axis_projection_sign(self):
        axis = np.array([1.0, 0.0, 0.0])
        v = np.array([0.6, 0.3, -0.2])
        v_prime = rmr.householder_reflect(v, axis, 2.0)
        # Householder at alpha=2: projection on axis flips sign.
        assert v_prime[0] == pytest.approx(-0.6)
        # Orthogonal components are unchanged.
        assert v_prime[1] == pytest.approx(0.3)
        assert v_prime[2] == pytest.approx(-0.2)

    def test_alpha_1_zeroes_axis_projection(self):
        axis = np.array([0.0, 1.0, 0.0])
        v = np.array([0.4, 0.7, 0.1])
        v_prime = rmr.householder_reflect(v, axis, 1.0)
        # alpha=1 removes the component along axis (orthogonal projection).
        assert v_prime[1] == pytest.approx(0.0)
        assert v_prime[0] == pytest.approx(0.4)

    def test_alpha_0_is_identity(self):
        axis = np.array([0.0, 0.0, 1.0])
        v = np.array([0.1, -0.2, 0.5])
        v_prime = rmr.householder_reflect(v, axis, 0.0)
        np.testing.assert_allclose(v_prime, v)


class TestFitAxisFromSeeds:
    def test_centroid_then_normalise(self):
        seeds = [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [1.1, -0.1, 0.0]]
        axis = rmr.fit_axis_from_seeds(seeds)
        assert np.linalg.norm(axis) == pytest.approx(1.0)
        # All seeds point along +x; the centroid should too.
        assert axis[0] > 0.99

    def test_zero_centroid_raises(self):
        seeds = [[1.0, 0.0], [-1.0, 0.0]]
        with pytest.raises(rmr.RigorError):
            rmr.fit_axis_from_seeds(seeds)


class TestRecoveryRate:
    def test_perfect_axis_perfect_recovery(self):
        axis = np.array([1.0, 0.0, 0.0])
        holdout = [
            {"embedding": [0.7, 0.1, 0.0], "ideology_label": 1},
            {"embedding": [-0.6, -0.05, 0.0], "ideology_label": -1},
        ]
        rate, successes = rmr.recovery_rate(
            holdout, reflect_axis=axis, alpha=2.0, eval_axis=axis,
        )
        assert rate == 1.0
        assert successes == [1, 1]

    def test_random_axis_does_not_flip(self):
        # If reflect_axis is orthogonal to eval_axis, the reflected
        # projection on eval_axis is unchanged (no flip), so recovery
        # is 0 for sentences whose label expects the opposite side.
        eval_axis = np.array([1.0, 0.0, 0.0])
        reflect_axis = np.array([0.0, 1.0, 0.0])
        holdout = [
            {"embedding": [0.7, 0.4, 0.0], "ideology_label": 1},
            {"embedding": [-0.6, -0.5, 0.0], "ideology_label": -1},
        ]
        rate, _ = rmr.recovery_rate(
            holdout, reflect_axis=reflect_axis, alpha=2.0,
            eval_axis=eval_axis,
        )
        assert rate == 0.0


class TestSampleRandomUnitVector:
    def test_unit_norm_and_dim(self):
        import random as _random
        rng = _random.Random(7)
        for _ in range(20):
            v = rmr.sample_random_unit_vector(rng, 16)
            assert v.shape == (16,)
            assert np.linalg.norm(v) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------


class TestBootstrapRecoveryCi:
    def test_all_successes_gives_unit_ci(self):
        ci_low, ci_high, mean, std = rmr.bootstrap_recovery_ci(
            [1] * 50, iterations=200, seed=11,
        )
        assert ci_low == 1.0
        assert ci_high == 1.0
        assert mean == 1.0
        assert std == 0.0

    def test_all_failures_gives_zero_ci(self):
        ci_low, ci_high, mean, std = rmr.bootstrap_recovery_ci(
            [0] * 50, iterations=200, seed=11,
        )
        assert ci_low == 0.0
        assert ci_high == 0.0
        assert mean == 0.0
        assert std == 0.0

    def test_balanced_inputs_centered_on_half(self):
        ci_low, ci_high, mean, std = rmr.bootstrap_recovery_ci(
            [1, 0] * 100, iterations=500, seed=11,
        )
        assert 0.4 < mean < 0.6
        assert ci_low < ci_high
        assert std > 0.0


# ---------------------------------------------------------------------------
# Pre-registration parsing
# ---------------------------------------------------------------------------


def test_preregistration_loads_and_has_required_keys():
    prereg = rmr.load_preregistration()
    assert prereg["version"] == "v1.0"
    assert prereg["random_seed"] == 50
    assert prereg["n_bootstrap"] == 10000
    assert prereg["n_random_axes"] == 100
    assert prereg["alpha_grid"] == [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    assert prereg["stopping_rule"]["minimum_holdout_sentences"] == 50


def test_preregistration_pins_alpha_2_as_headline():
    prereg = rmr.load_preregistration()
    assert 2.0 in prereg["alpha_grid"], (
        "alpha=2 (the original Householder reflection) MUST be in the "
        "frozen grid; it is the headline against which recovery is reported."
    )


def test_preregistration_lists_held_out_protocol_and_random_baseline():
    prereg = rmr.load_preregistration()
    assert "held_out_protocol" in prereg
    assert "random_baseline" in prereg
    prohibited = prereg.get("prohibited_actions") or []
    assert any("post-hoc alpha" in p for p in prohibited)
    assert any(
        "single point estimate" in p for p in prohibited
    )


# ---------------------------------------------------------------------------
# End-to-end harness on the synthetic fixture
# ---------------------------------------------------------------------------


def test_dry_run_recovers_at_alpha_2_and_baseline_is_well_separated():
    """Synthetic where reflection is well-defined: held-out recovery at
    alpha=2 is near 1.0; the random-axis baseline is far below the
    held-out CI so the decision rule fires."""
    report = rmr.run_rigor_study(_dry_run_config())
    held = report.held_out_recovery["by_alpha"]["alpha_2.0000"]
    base = report.random_baseline["by_alpha"]["alpha_2.0000"]
    assert held["recovery_rate"] >= 0.95, (
        f"held-out recovery at alpha=2 should be near 1.0, got "
        f"{held['recovery_rate']!r}"
    )
    assert base["mean"] < 0.5, (
        f"random-axis baseline mean at alpha=2 should be far below the "
        f"held-out value, got {base['mean']!r}"
    )
    assert held["ci_low"] > base["ci_high"], (
        f"held-out CI low ({held['ci_low']}) must exceed random-baseline "
        f"CI high ({base['ci_high']}) for the primary rule to fire"
    )


def test_dry_run_decision_block_reports_generalisation():
    report = rmr.run_rigor_study(_dry_run_config())
    decision = report.decision
    assert decision["headline_alpha"] == 2.0
    assert decision["primary_generalises"] is True
    assert (
        decision["primary_outcome"]
        == "reflection_recovery_generalises_held_out"
    )
    assert "primary_margin" in decision
    assert decision["primary_margin"] > 0.0


def test_dry_run_alpha_sweep_includes_full_grid():
    report = rmr.run_rigor_study(_dry_run_config())
    expected = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)
    for alpha in expected:
        key = f"alpha_{alpha:.4f}"
        assert key in report.held_out_recovery["by_alpha"]
        assert key in report.random_baseline["by_alpha"]


def test_dry_run_is_byte_identical_across_runs():
    a = rmr.run_rigor_study(_dry_run_config()).to_canonical_bytes()
    b = rmr.run_rigor_study(_dry_run_config()).to_canonical_bytes()
    assert a == b, (
        "rigor harness must produce byte-identical canonical output "
        "for the same RigorConfig + same input fixture"
    )


def test_changing_seed_changes_random_baseline_but_not_axis():
    a = rmr.run_rigor_study(_dry_run_config(seed=50))
    b = rmr.run_rigor_study(_dry_run_config(seed=51))
    # Fitted axis depends only on training seeds, not config.seed
    assert a.inputs["axis_norm"] == b.inputs["axis_norm"]
    # Random-axis sampling is seeded — different seeds give different baselines
    a_base = a.random_baseline["by_alpha"]["alpha_2.5000"]
    b_base = b.random_baseline["by_alpha"]["alpha_2.5000"]
    assert (a_base["min"], a_base["max"]) != (b_base["min"], b_base["max"]) \
        or a_base["mean"] != b_base["mean"]


def test_inputs_digest_changes_when_corpus_changes(tmp_path):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    digest_a = rmr.run_rigor_study(_dry_run_config()).inputs[
        "corpus_digest_sha256"
    ]
    perturbed = json.loads(json.dumps(fixture))
    perturbed["holdout_corpus"]["sentences"][0]["embedding"][0] = (
        float(perturbed["holdout_corpus"]["sentences"][0]["embedding"][0])
        + 0.5
    )
    p = tmp_path / "perturbed.json"
    p.write_text(json.dumps(perturbed), encoding="utf-8")
    digest_b = rmr.run_rigor_study(
        _dry_run_config(fixture_path=str(p))
    ).inputs["corpus_digest_sha256"]
    assert digest_a != digest_b


def test_held_out_leakage_detected(tmp_path):
    """If a holdout sentence's embedding equals an axis seed, the
    harness must refuse to run the held-out evaluation."""
    payload = {
        "schema": "reverse-marxism-rigor-fixture-v1",
        "source": "fixture",
        "model_id": "test", "model_version": "0",
        "dim": 4,
        "training_corpus": {
            "axis_seed_embeddings": [
                [1.0, 0.0, 0.0, 0.0],
                [0.9, 0.1, 0.0, 0.0],
            ],
        },
        "holdout_corpus": {
            "sentences": [
                {"embedding": [1.0, 0.0, 0.0, 0.0], "ideology_label": 1},
                {"embedding": [-1.0, 0.0, 0.0, 0.0], "ideology_label": -1},
            ],
        },
    }
    p = tmp_path / "leaky.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rmr.HeldOutLeakageError):
        rmr.run_rigor_study(_dry_run_config(fixture_path=str(p)))


def test_insufficient_holdout_raises_on_real_corpus(tmp_path):
    payload = {
        "schema": "reverse-marxism-rigor-fixture-v1",
        "source": "real",  # not "fixture" -> stopping rule applies
        "model_id": "test", "model_version": "0",
        "dim": 4,
        "training_corpus": {
            "axis_seed_embeddings": [
                [1.0, 0.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0],
                [0.85, 0.0, 0.05, 0.0], [0.92, -0.05, 0.0, 0.0],
                [0.88, 0.0, -0.02, 0.0],
            ],
        },
        "holdout_corpus": {
            "sentences": [
                {"embedding": [0.7, 0.1, 0.0, 0.0], "ideology_label": 1},
                {"embedding": [-0.6, 0.0, 0.05, 0.0], "ideology_label": -1},
            ],
        },
    }
    p = tmp_path / "tiny.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    config = rmr.RigorConfig(
        seed=50, n_random_axes=4, n_bootstrap=10,
        corpus_path=str(p),
    )
    with pytest.raises(rmr.InsufficientHoldoutError):
        rmr.run_rigor_study(config)


def test_missing_fixture_and_corpus_raises():
    with pytest.raises(rmr.RigorError):
        rmr.run_rigor_study(rmr.RigorConfig(seed=50))


def test_invalid_ideology_label_rejected(tmp_path):
    payload = {
        "schema": "reverse-marxism-rigor-fixture-v1",
        "source": "fixture",
        "model_id": "test", "model_version": "0",
        "dim": 3,
        "training_corpus": {
            "axis_seed_embeddings": [[1.0, 0.0, 0.0], [0.9, 0.0, 0.1]],
        },
        "holdout_corpus": {
            # Label 0 is not in {-1, +1}
            "sentences": [
                {"embedding": [0.5, 0.1, 0.0], "ideology_label": 0},
            ],
        },
    }
    p = tmp_path / "bad-label.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rmr.RigorError):
        rmr.run_rigor_study(_dry_run_config(fixture_path=str(p)))


def test_report_has_all_required_blocks():
    report = rmr.run_rigor_study(_dry_run_config())
    d = report.to_canonical_dict()
    for key in (
        "schema_version", "run_id", "config", "preregistration",
        "inputs", "held_out_recovery", "random_baseline",
        "decision", "generated_with",
    ):
        assert key in d
    # Every alpha in the grid carries a CI on both blocks (the
    # prohibition on point estimates without CIs).
    for key in ("alpha_0.5000", "alpha_1.0000", "alpha_1.5000",
                "alpha_2.0000", "alpha_2.5000", "alpha_3.0000"):
        held = d["held_out_recovery"]["by_alpha"][key]
        base = d["random_baseline"]["by_alpha"][key]
        for field in ("ci_low", "ci_high", "ci_percent"):
            assert field in held, (
                f"held-out {key} missing {field}; the no-point-estimate-"
                f"without-CI guard requires it."
            )
            assert field in base


def test_schema_version_pinned():
    assert rmr.RIGOR_SCHEMA_VERSION == "reverse-marxism-rigor-v1"


def test_decision_uses_empirical_baseline_not_idealised_05():
    """The decision rule must compare against the empirical random-
    baseline CI high, not the idealised 0.5 null. The rigor study
    exists precisely to make that comparison data-driven."""
    report = rmr.run_rigor_study(_dry_run_config())
    head = report.held_out_recovery["by_alpha"]["alpha_2.0000"]
    base = report.random_baseline["by_alpha"]["alpha_2.0000"]
    decision = report.decision
    assert (
        decision["primary_random_baseline_ci_high"] == base["ci_high"]
    ), (
        "decision must use empirical CI of random baseline, "
        "not 0.5"
    )
    assert decision["primary_held_out_ci_low"] == head["ci_low"]


# ---------------------------------------------------------------------------
# CLI integration (smoke)
# ---------------------------------------------------------------------------


def test_cli_dry_run_emits_report(tmp_path):
    repo = Path(rmr.__file__).resolve().parents[2]  # /<...>/coherence_engine
    parent = repo.parent
    out_path = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "coherence_engine", "replication",
         "reverse-marxism-rigor", "--dry-run", "--output", str(out_path)],
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
    assert payload["schema_version"] == rmr.RIGOR_SCHEMA_VERSION
    assert payload["decision"]["headline_alpha"] == 2.0
    assert (
        payload["decision"]["primary_outcome"]
        == "reflection_recovery_generalises_held_out"
    )
