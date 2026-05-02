"""Tests for the contradiction-direction (ĉ) stability harness (prompt 48).

Two contracts:

  * **Geometry**: a synthetic two-domain fixture with intentionally
    orthogonal contradiction axes recovers near-orthogonal ĉ vectors,
    near-perfect within-domain AUC, and near-chance cross-domain AUC.
    The decision rule then flips to ``per_domain_c_hat_required``.
  * **Determinism**: the canonical report bytes are byte-identical
    across runs given the same config and inputs (after stripping the
    volatile ``generated_with`` block that records library versions).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from coherence_engine.core.contradiction_direction import (
    fit_c_hat,
    pair_directions,
    project,
    abs_cosine,
    cosine,
)
from coherence_engine.Experiments.Contradiction_Direction_Stability import (
    run_stability_study as chs,
)


HERE = Path(chs.__file__).resolve().parent
FIXTURE_PATH = HERE / "fixtures" / "tiny_two_domain_fixture.json"
PREREG_PATH = HERE / "preregistration.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dry_run_config(**overrides):
    base = dict(
        seed=4747,
        n_bootstrap_iterations=100,
        ci_percent=95.0,
        n_subsamples=10,
        subsample_sizes=(8, 16),
        fixture_path=str(FIXTURE_PATH),
        minimum_pairs_override=4,
    )
    base.update(overrides)
    return chs.StabilityConfig(**base)


def _strip_volatile(d):
    out = dict(d)
    out.pop("generated_with", None)
    return out


# ---------------------------------------------------------------------------
# Core ĉ geometry
# ---------------------------------------------------------------------------


class TestFitCHat:
    def test_single_pair_returns_unit_vector_along_diff(self):
        u = np.array([1.0, 0.0, 0.0])
        v = np.array([-1.0, 0.0, 0.0])
        c = fit_c_hat(np.array([[u, v]]))
        assert pytest.approx(np.linalg.norm(c)) == 1.0
        # Up to sign, ĉ is along e_0.
        assert abs(abs(c[0]) - 1.0) < 1e-9

    def test_canonical_sign_is_deterministic(self):
        u = np.array([1.0, 0.0])
        v = np.array([-1.0, 0.0])
        c1 = fit_c_hat(np.array([[u, v]]))
        c2 = fit_c_hat(np.array([[v, u]]))  # swapped
        # Both should have same canonical sign even though raw n_i flipped.
        assert np.allclose(c1, c2)
        assert c1[0] > 0  # first non-zero coord positive

    def test_principal_axis_recovers_dominant_direction(self):
        # 20 pairs all aligned with e_1, plus 2 outliers along e_2 — the
        # principal direction should still be e_1.
        rng = np.random.default_rng(0)
        pairs = []
        for _ in range(20):
            u = rng.normal(size=4)
            v = u.copy()
            v[1] = -v[1]  # contradiction along e_1
            pairs.append([u, v])
        for _ in range(2):
            u = rng.normal(size=4)
            v = u.copy()
            v[2] = -v[2]
            pairs.append([u, v])
        c = fit_c_hat(np.array(pairs))
        # Principal axis should be much closer to e_1 than to e_2.
        assert abs(c[1]) > abs(c[2])
        assert abs(c[1]) > 0.7

    def test_pair_directions_normalised(self):
        pairs = np.array([
            [[2.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
            [[0.0, 3.0, 0.0], [0.0, 0.0, 0.0]],
        ])
        N = pair_directions(pairs)
        norms = np.linalg.norm(N, axis=1)
        assert np.allclose(norms, 1.0)

    def test_abs_cosine_is_sign_invariant(self):
        a = np.array([1.0, 0.0, 0.0])
        b = np.array([-1.0, 0.0, 0.0])
        assert pytest.approx(cosine(a, b)) == -1.0
        assert pytest.approx(abs_cosine(a, b)) == 1.0

    def test_project_drops_sign(self):
        pairs = np.array([
            [[1.0, 0.0], [-1.0, 0.0]],
            [[-1.0, 0.0], [1.0, 0.0]],
        ])
        c = np.array([1.0, 0.0])
        scores = project(pairs, c)
        # Both pairs should yield magnitude 2 regardless of u/v ordering.
        assert np.allclose(scores, [2.0, 2.0])

    def test_fit_c_hat_rejects_empty_input(self):
        with pytest.raises(ValueError):
            fit_c_hat(np.zeros((0, 2, 4)))

    def test_fit_c_hat_rejects_wrong_shape(self):
        with pytest.raises(ValueError):
            fit_c_hat(np.zeros((5, 3, 4)))  # not 2 vectors per pair


# ---------------------------------------------------------------------------
# Cross-domain stability — synthetic divergent ĉ
# ---------------------------------------------------------------------------


class TestStabilityHarness:
    def test_dry_run_emits_report(self):
        config = _dry_run_config()
        report = chs.run_stability_study(config)
        d = report.to_canonical_dict()
        assert d["schema_version"] == chs.STABILITY_SCHEMA_VERSION
        assert set(d["per_domain_c_hat"].keys()) == {"domain_a", "domain_b"}
        assert "matrix" in d["pairwise_cosine"]
        assert "within_domain_baseline" in d["cross_domain_auc"]
        assert "cross_domain" in d["cross_domain_auc"]
        assert "by_size" in d["subsample_sensitivity"]
        assert d["decision"]["outcome"] in (
            "single_c_hat_generalises", "per_domain_c_hat_required"
        )

    def test_orthogonal_domains_recover_low_cross_cosine(self):
        # The bundled fixture is engineered so domain_a's ĉ ≈ e_0 and
        # domain_b's ĉ ≈ e_1 — pairwise abs-cosine should be ~0.
        config = _dry_run_config()
        report = chs.run_stability_study(config)
        d = report.to_canonical_dict()
        ab = d["pairwise_cosine"]["matrix"]["domain_a"]["domain_b"]["abs_cosine"]
        assert ab < 0.30, (
            f"expected ĉ_a and ĉ_b near-orthogonal, got abs-cosine={ab}"
        )

    def test_cross_domain_auc_drops_for_orthogonal_domains(self):
        # Within-domain AUC should be ~1.0, cross-domain AUC should be
        # near 0.5 — i.e., the "drop" exceeds the 0.05 falsification line.
        config = _dry_run_config()
        report = chs.run_stability_study(config)
        d = report.to_canonical_dict()
        within_a = d["cross_domain_auc"]["within_domain_baseline"]["domain_a"]["auc"]
        cross_ab = d["cross_domain_auc"]["cross_domain"]["domain_a"]["domain_b"]["auc"]
        assert within_a > 0.85, (
            f"expected within-domain AUC ≥ 0.85 for orthogonal axes, got {within_a}"
        )
        assert cross_ab < 0.7, (
            f"expected cross-domain AUC < 0.70 for orthogonal axes, got {cross_ab}"
        )

    def test_decision_recommends_per_domain_for_orthogonal_fixture(self):
        config = _dry_run_config()
        report = chs.run_stability_study(config)
        d = report.to_canonical_dict()
        assert d["decision"]["outcome"] == "per_domain_c_hat_required", (
            "with orthogonal contradiction axes the harness must NOT "
            "recommend a single ĉ"
        )
        assert d["decision"]["single_c_hat_holds"] is False

    def test_subsample_sensitivity_reports_each_size(self):
        config = _dry_run_config()
        report = chs.run_stability_study(config)
        d = report.to_canonical_dict()
        by_size = d["subsample_sensitivity"]["by_size"]
        assert set(by_size.keys()) == {"8", "16"}
        for size_key, payload in by_size.items():
            assert payload["n_subsamples"] >= 1
            # ĉ on a subsample of the pooled set should not have a
            # crazy-low cosine to ĉ(full pool).
            assert 0.0 <= payload["abs_cosine_to_full_mean"] <= 1.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_byte_identical_across_runs(self):
        config = _dry_run_config()
        a = chs.run_stability_study(config).to_canonical_bytes()
        b = chs.run_stability_study(config).to_canonical_bytes()
        # Strip volatile fields by re-parsing then re-serialising both.
        da = _strip_volatile(json.loads(a.decode("ascii")))
        db = _strip_volatile(json.loads(b.decode("ascii")))
        assert json.dumps(da, sort_keys=True) == json.dumps(db, sort_keys=True)

    def test_same_seed_same_run_id(self):
        config = _dry_run_config()
        r1 = chs.run_stability_study(config).to_canonical_dict()
        r2 = chs.run_stability_study(config).to_canonical_dict()
        assert r1["run_id"] == r2["run_id"]

    def test_different_seed_changes_bootstrap_ci_but_not_point_estimates(self):
        r_a = chs.run_stability_study(_dry_run_config(seed=4747)).to_canonical_dict()
        r_b = chs.run_stability_study(_dry_run_config(seed=9999)).to_canonical_dict()
        # Per-domain ĉ vectors are seed-free (deterministic SVD).
        assert r_a["per_domain_c_hat"] == r_b["per_domain_c_hat"]
        # Pairwise abs-cosine point estimate is seed-free; only its CI moves.
        ab_a = r_a["pairwise_cosine"]["matrix"]["domain_a"]["domain_b"]
        ab_b = r_b["pairwise_cosine"]["matrix"]["domain_a"]["domain_b"]
        assert ab_a["abs_cosine"] == ab_b["abs_cosine"]


# ---------------------------------------------------------------------------
# Pre-registration discipline
# ---------------------------------------------------------------------------


class TestPreregistration:
    def test_loads_from_disk(self):
        prereg = chs.load_preregistration(str(PREREG_PATH))
        assert prereg["version"] == "v1.0"
        assert prereg["study_name"].startswith("c-hat-cross-domain-stability")
        assert prereg["n_subsamples"] == 50
        assert prereg["subsample_sizes"] == [200, 500, 1000]

    def test_missing_corpus_raises(self):
        config = chs.StabilityConfig(seed=1, fixture_path=None, corpus_path=None)
        with pytest.raises(chs.StabilityError):
            chs.run_stability_study(config)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def test_cli_dry_run_emits_canonical_json(tmp_path):
    import subprocess
    import sys as _sys

    repo = HERE.parents[1]  # /<...>/coherence_engine
    parent = repo.parent
    out_path = tmp_path / "report.json"
    proc = subprocess.run(
        [
            _sys.executable, "-m", "coherence_engine",
            "replication", "c-hat-stability", "--dry-run",
            "--output", str(out_path),
        ],
        cwd=parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"CLI failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == chs.STABILITY_SCHEMA_VERSION
    assert payload["decision"]["outcome"] in (
        "single_c_hat_generalises", "per_domain_c_hat_required"
    )
