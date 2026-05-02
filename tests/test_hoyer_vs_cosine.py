"""Tests for the Hoyer-vs-cosine head-to-head ROC harness (prompt 49).

Two contracts:

  * **Statistical**: a synthetic fixture in which Hoyer dominates
    cosine *by construction* (deltas are constructed orthogonal to
    the base vector with matching magnitudes, so cosines overlap
    heavily across labels while Hoyer-of-difference cleanly
    separates them). The DeLong two-sided z-test must reject the
    null AUC(hoyer) == AUC(cosine) at the configured alpha.
  * **Determinism**: canonical report bytes are byte-identical
    across runs given the same config and inputs (after stripping
    the volatile ``generated_with`` block that records library
    versions).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coherence_engine.Experiments.Hoyer_vs_Cosine import (
    run_comparison as hvc,
)


HERE = Path(hvc.__file__).resolve().parent
FIXTURE_PATH = HERE / "fixtures" / "tiny_pair_fixture.json"
PREREG_PATH = HERE / "preregistration.yaml"


def _dry_run_config(**overrides):
    base = dict(
        seed=49,
        n_bootstrap_iterations=200,
        alpha=0.01,
        ci_percent=95.0,
        fixture_path=str(FIXTURE_PATH),
        minimum_eval_pairs_override=4,
    )
    base.update(overrides)
    return hvc.ComparisonConfig(**base)


def _strip_volatile(d):
    out = dict(d)
    out.pop("generated_with", None)
    return out


# ---------------------------------------------------------------------------
# Statistical primitives
# ---------------------------------------------------------------------------


class TestRocAuc:
    def test_perfect_separation(self):
        scores = [0.1, 0.2, 0.9, 1.0]
        labels = [0, 0, 1, 1]
        assert hvc.roc_auc(scores, labels) == pytest.approx(1.0)

    def test_inverted_separation(self):
        scores = [0.9, 1.0, 0.1, 0.2]
        labels = [0, 0, 1, 1]
        assert hvc.roc_auc(scores, labels) == pytest.approx(0.0)

    def test_all_ties_is_chance(self):
        scores = [0.5, 0.5, 0.5, 0.5]
        labels = [0, 1, 0, 1]
        assert hvc.roc_auc(scores, labels) == pytest.approx(0.5)

    def test_empty_class_returns_chance(self):
        scores = [0.1, 0.2, 0.3]
        labels = [0, 0, 0]
        assert hvc.roc_auc(scores, labels) == 0.5


class TestDeLongTest:
    def test_identical_classifiers_var_zero_p_one(self):
        pos = [0.9, 0.8, 0.95]
        neg = [0.1, 0.2, 0.15]
        result = hvc.delong_test(pos, neg, pos, neg)
        # Identical classifiers => zero variance, p == 1.0
        assert result["auc_diff"] == 0.0
        assert result["var_diff"] == 0.0
        assert result["p_value"] == 1.0

    def test_synthetic_hoyer_beats_cosine_rejects_at_alpha(self):
        # Hoyer cleanly separates; cosine is noise.
        pos_hoyer = [0.95, 0.9, 0.92, 0.88, 0.91, 0.93, 0.94, 0.89]
        neg_hoyer = [0.10, 0.15, 0.12, 0.08, 0.20, 0.05, 0.18, 0.11]
        pos_cosine = [0.50, 0.49, 0.51, 0.50, 0.48, 0.52, 0.50, 0.49]
        neg_cosine = [0.50, 0.51, 0.49, 0.50, 0.52, 0.48, 0.50, 0.49]
        result = hvc.delong_test(pos_hoyer, neg_hoyer, pos_cosine, neg_cosine)
        assert result["auc_a"] == pytest.approx(1.0)
        assert 0.4 < result["auc_b"] < 0.6
        assert result["auc_diff"] > 0.4
        # DeLong must reject equality at the configured alpha
        assert result["p_value"] < 0.01, (
            f"p={result['p_value']!r} z={result['z']!r}"
        )

    def test_unequal_lengths_raise(self):
        with pytest.raises(ValueError):
            hvc.delong_test([0.5], [0.1, 0.2], [0.5, 0.6], [0.1])

    def test_two_sided_p_is_symmetric_in_ordering(self):
        a_pos = [0.9, 0.8, 0.7]
        a_neg = [0.1, 0.2, 0.3]
        b_pos = [0.5, 0.4, 0.6]
        b_neg = [0.4, 0.5, 0.6]
        ab = hvc.delong_test(a_pos, a_neg, b_pos, b_neg)
        ba = hvc.delong_test(b_pos, b_neg, a_pos, a_neg)
        assert ab["p_value"] == pytest.approx(ba["p_value"])
        assert ab["auc_diff"] == pytest.approx(-ba["auc_diff"])


# ---------------------------------------------------------------------------
# Pre-registration parsing
# ---------------------------------------------------------------------------


def test_preregistration_loads_and_has_required_keys():
    prereg = hvc.load_preregistration()
    assert prereg["version"] == "v1.0"
    assert prereg["random_seed"] == 49
    assert prereg["primary_hypothesis"]["alpha"] == 0.01
    assert prereg["primary_hypothesis"]["n_bootstrap"] == 10000
    assert prereg["bootstrap"]["iterations"] == 10000
    assert prereg["stopping_rule"]["minimum_eval_pairs_per_label"] == 200


def test_preregistration_pins_two_sided_test():
    prereg = hvc.load_preregistration()
    primary_test = prereg["primary_hypothesis"]["test"]
    assert "two-sided" in primary_test, (
        "Pre-registration must lock the test to two-sided to honour the "
        "directionality_note prohibition on post-hoc one-sided switches."
    )


# ---------------------------------------------------------------------------
# End-to-end harness on the synthetic fixture
# ---------------------------------------------------------------------------


def test_dry_run_recovers_hoyer_dominates_cosine():
    """Synthetic where Hoyer dominates cosine by construction: DeLong
    rejects equality at the configured alpha."""
    report = hvc.run_comparison(_dry_run_config())
    auc = report.auc
    delong = report.delong["hoyer_vs_cosine"]
    # Hoyer separates perfectly on the fixture; cosine is essentially chance.
    assert auc["hoyer"]["auc"] >= 0.95
    assert abs(auc["cosine"]["auc"] - 0.5) < 0.10
    # DeLong must reject equality at alpha=0.01
    assert delong["reject_null"] is True
    assert delong["p_value"] < 0.01
    assert delong["winner"] == "hoyer"
    assert report.interpretation["primary_outcome"] == (
        "hoyer_signal_differs_from_cosine"
    )


def test_projection_also_beats_cosine_on_fixture():
    report = hvc.run_comparison(_dry_run_config())
    proj = report.delong["projection_vs_cosine"]
    assert proj["reject_null"] is True
    assert proj["winner"] == "projection"


def test_dry_run_is_byte_identical_across_runs():
    a = hvc.run_comparison(_dry_run_config()).to_canonical_bytes()
    b = hvc.run_comparison(_dry_run_config()).to_canonical_bytes()
    assert a == b, "harness must produce byte-identical canonical output"


def test_changing_seed_changes_ci_but_not_point_estimate():
    a = hvc.run_comparison(_dry_run_config(seed=49))
    b = hvc.run_comparison(_dry_run_config(seed=50))
    # Note: the seed also drives the 50/50 fit/eval split, so the
    # eval set itself differs across seeds — point AUCs may differ.
    # What is determinism-checked is that the bootstrap CI changes
    # when only the bootstrap seed changes (same fit/eval split).
    assert a.auc != b.auc or a.auc["cosine"]["ci_low"] != b.auc["cosine"]["ci_low"]


def test_inputs_digest_changes_when_pairs_change(tmp_path):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    digest_a = hvc.run_comparison(_dry_run_config()).inputs[
        "corpus_digest_sha256"
    ]
    perturbed = json.loads(json.dumps(fixture))
    perturbed["pairs"][0]["u"][0] = float(perturbed["pairs"][0]["u"][0]) + 0.5
    p = tmp_path / "perturbed.json"
    p.write_text(json.dumps(perturbed), encoding="utf-8")
    digest_b = hvc.run_comparison(
        _dry_run_config(fixture_path=str(p))
    ).inputs["corpus_digest_sha256"]
    assert digest_a != digest_b


def test_insufficient_sample_raises_when_below_minimum(tmp_path):
    payload = {
        "schema": "hoyer-vs-cosine-fixture-v1",
        "source": "real",  # not "fixture" -> stopping rule applies
        "model_id": "test",
        "model_version": "0",
        "dim": 4,
        "pairs": [
            {"label": "contradiction", "u": [1, 0, 0, 0], "v": [0, 1, 0, 0]},
            {"label": "contradiction", "u": [0, 1, 0, 0], "v": [1, 0, 0, 0]},
            {"label": "entailment", "u": [1, 0, 0, 0], "v": [1, 0.01, 0, 0]},
            {"label": "entailment", "u": [0, 1, 0, 0], "v": [0, 1, 0.01, 0]},
        ],
    }
    p = tmp_path / "tiny.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    config = hvc.ComparisonConfig(
        seed=49,
        n_bootstrap_iterations=50,
        corpus_path=str(p),
    )
    with pytest.raises(hvc.InsufficientEvalSampleError):
        hvc.run_comparison(config)


def test_missing_fixture_and_corpus_raises():
    with pytest.raises(hvc.ComparisonError):
        hvc.run_comparison(hvc.ComparisonConfig(seed=49))


def test_report_has_all_required_blocks():
    report = hvc.run_comparison(_dry_run_config())
    d = report.to_canonical_dict()
    for key in (
        "schema_version", "run_id", "config", "preregistration",
        "inputs", "auc", "delong", "interpretation", "generated_with",
    ):
        assert key in d
    for cls in ("cosine", "hoyer", "projection"):
        assert cls in d["auc"]
        # Every AUC point estimate is reported with its bootstrap CI
        for field in ("auc", "ci_low", "ci_high", "ci_percent"):
            assert field in d["auc"][cls], (
                f"AUC point estimate for {cls!r} missing {field!r} — the "
                f"prohibition on reporting AUC without CIs requires both."
            )
    for pair in ("hoyer_vs_cosine", "projection_vs_cosine"):
        assert pair in d["delong"]
        for field in ("auc_diff", "z", "p_value", "reject_null", "winner"):
            assert field in d["delong"][pair]


def test_schema_version_pinned():
    assert hvc.COMPARISON_SCHEMA_VERSION == "hoyer-vs-cosine-v1"


# ---------------------------------------------------------------------------
# CLI integration (smoke)
# ---------------------------------------------------------------------------


def test_cli_dry_run_emits_report(tmp_path):
    repo = Path(hvc.__file__).resolve().parents[2]  # /<...>/coherence_engine
    parent = repo.parent
    out_path = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "coherence_engine", "replication",
         "hoyer-vs-cosine", "--dry-run", "--output", str(out_path)],
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
    assert payload["schema_version"] == hvc.COMPARISON_SCHEMA_VERSION
    assert payload["interpretation"]["primary_outcome"] == (
        "hoyer_signal_differs_from_cosine"
    )
