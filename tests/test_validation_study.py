"""Tests for the coherence-vs-outcome regression study harness (prompt 44).

Covers:

* Determinism: two runs at the same seed produce byte-identical reports.
* Power: on a synthetic frame with a planted positive coefficient on
  ``coherence_score``, the harness recovers the sign and rejects H0 at
  the pre-registered alpha=0.01 with high probability across multiple
  fixed seeds.
* Stopping rule: ``run_study`` raises ``InsufficientSampleError`` when
  the joined frame has fewer rows than the pre-registered minimum.
* Pre-registration parser: required keys are enforced; unknown
  indentation raises ``PreregistrationError``.
* Pure metrics: Brier, AUC, calibration, percentile, and the IRLS
  logistic regression behave correctly on hand-checkable cases.
* Markdown renderer: produces output that mentions the report's headline
  facts (n, AUC, Brier, primary-result decision).
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from coherence_engine.server.fund.services import validation_report as vr
from coherence_engine.server.fund.services.validation_study import (
    InsufficientSampleError,
    PreregistrationError,
    StudyConfig,
    StudyRow,
    auc_roc,
    brier_score,
    calibration_curve,
    fit_logit,
    load_preregistration,
    run_study,
)


# ---------------------------------------------------------------------------
# Synthetic frame helpers
# ---------------------------------------------------------------------------


_DOMAINS = ("fintech", "healthtech", "deeptech", "consumer")


def _planted_frame(
    *,
    n: int,
    seed: int,
    beta_coh: float = 4.0,
    intercept: float = -1.0,
) -> list:
    """Generate a synthetic study frame with a planted positive effect.

    ``coherence_score`` is uniform in [0, 1]; survival is drawn from a
    Bernoulli with logit-linear mean. Same seed ⇒ same frame.
    """

    rng = random.Random(seed)
    rows = []
    for i in range(n):
        coh = rng.random()
        domain = _DOMAINS[i % len(_DOMAINS)]
        check = 25_000 + (i % 10) * 25_000  # 25k .. 250k
        # logit-linear with planted effect on coherence
        logit_p = intercept + beta_coh * coh
        p = 1.0 / (1.0 + math.exp(-logit_p))
        survived = 1 if rng.random() < p else 0
        pid = f"{i:08x}-{seed:04x}-7000-8000-{rng.randrange(16**12):012x}"
        rows.append(
            StudyRow(
                pitch_id=pid,
                domain=domain,
                coherence_score=coh,
                check_size_usd=check,
                survival_5yr=survived,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Pure-metric tests
# ---------------------------------------------------------------------------


def test_brier_score_perfect_and_worst() -> None:
    assert brier_score([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0]) == 0.0
    # perfectly wrong
    assert brier_score([0.0, 1.0, 0.0, 1.0], [1, 0, 1, 0]) == 1.0


def test_auc_roc_perfect_and_random() -> None:
    # perfect ranking
    assert auc_roc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == pytest.approx(1.0)
    # all-tied predictions ⇒ rank-based AUC = 0.5
    assert auc_roc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == pytest.approx(0.5)


def test_calibration_curve_shape() -> None:
    bins = calibration_curve([0.05, 0.15, 0.95], [0, 0, 1], n_bins=10)
    # Always 10 bins, even when most are empty.
    assert len(bins) == 10
    # bin 0 holds 0.05; bin 1 holds 0.15; bin 9 holds 0.95.
    nonempty = {b.bin_index: b for b in bins if b.count > 0}
    assert set(nonempty.keys()) == {0, 1, 9}
    assert nonempty[9].mean_realized == 1.0
    assert nonempty[0].mean_realized == 0.0


def test_fit_logit_recovers_sign_on_clean_data() -> None:
    # Build a tiny dataset where x is strongly predictive of y.
    rng = random.Random(0)
    X = []
    y = []
    for _ in range(400):
        x1 = rng.gauss(0, 1)
        prob = 1.0 / (1.0 + math.exp(-(0.5 + 2.0 * x1)))
        yy = 1 if rng.random() < prob else 0
        X.append([1.0, x1])
        y.append(yy)
    beta, converged = fit_logit(X, y)
    assert converged
    # Intercept ~0.5, slope ~2.0 — we just check the sign is positive
    # and the magnitude is in the right ballpark.
    assert beta[1] > 1.0


# ---------------------------------------------------------------------------
# Pre-registration loader
# ---------------------------------------------------------------------------


def test_load_default_preregistration() -> None:
    pre = load_preregistration()
    assert pre["version"] == "v1.0"
    assert pre["primary_hypothesis"]["alpha"] == 0.01
    assert pre["stopping_rule"]["minimum_n_with_known_outcome"] == 200


def test_preregistration_missing_key_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        # missing 'primary_hypothesis', 'stopping_rule', etc.
        "version: v9\nstudy_name: x\n",
        encoding="utf-8",
    )
    with pytest.raises(PreregistrationError):
        load_preregistration(bad)


# ---------------------------------------------------------------------------
# Stopping rule
# ---------------------------------------------------------------------------


def test_run_study_raises_insufficient_sample() -> None:
    rows = _planted_frame(n=50, seed=1)
    cfg = StudyConfig(seed=0, bootstrap_iters=50)
    with pytest.raises(InsufficientSampleError) as exc:
        run_study(cfg, frame=rows)
    # The message must include the operator-greppable error code.
    assert "INSUFFICIENT_SAMPLE" in str(exc.value)
    assert exc.value.n == 50
    assert exc.value.minimum == 200


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_run_study_is_byte_deterministic(tmp_path: Path) -> None:
    rows = _planted_frame(n=240, seed=42)
    out1 = tmp_path / "r1.json"
    out2 = tmp_path / "r2.json"
    cfg1 = StudyConfig(seed=7, bootstrap_iters=200, output_path=out1)
    cfg2 = StudyConfig(seed=7, bootstrap_iters=200, output_path=out2)
    r1 = run_study(cfg1, frame=rows)
    r2 = run_study(cfg2, frame=rows)
    # Reports may differ in the resolved output_path; compare the *content*
    # written for r1 with the bytes that would be emitted for r2 if it had
    # the same output_path. We do this by zeroing out config.output_path
    # in one of the canonical dicts.
    assert r1.report_digest() != r2.report_digest()  # because output_path differs
    d1 = r1.to_canonical_dict()
    d2 = r2.to_canonical_dict()
    d1["config"]["output_path"] = None
    d2["config"]["output_path"] = None
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)
    # And same-output_path: re-run with output_path=out1 twice.
    cfg3 = StudyConfig(seed=7, bootstrap_iters=200, output_path=out1)
    r3 = run_study(cfg3, frame=rows)
    assert r1.report_digest() == r3.report_digest()
    assert out1.read_bytes() == r3.to_canonical_bytes()


def test_different_seed_changes_bootstrap_cis() -> None:
    rows = _planted_frame(n=240, seed=42)
    cfg_a = StudyConfig(seed=1, bootstrap_iters=300)
    cfg_b = StudyConfig(seed=2, bootstrap_iters=300)
    ra = run_study(cfg_a, frame=rows)
    rb = run_study(cfg_b, frame=rows)
    coh_a = next(c for c in ra.coefficients if c.name == "coherence_score")
    coh_b = next(c for c in rb.coefficients if c.name == "coherence_score")
    # Point estimate is determined by the data, not the seed.
    assert coh_a.point == coh_b.point
    # CIs are seeded → different seeds should not produce identical CIs.
    assert (coh_a.ci_lower_95, coh_a.ci_upper_95) != (
        coh_b.ci_lower_95,
        coh_b.ci_upper_95,
    )


# ---------------------------------------------------------------------------
# Power: planted-effect recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data_seed,boot_seed", [(11, 100), (22, 200), (33, 300)])
def test_planted_effect_is_recovered(data_seed: int, boot_seed: int) -> None:
    rows = _planted_frame(n=400, seed=data_seed, beta_coh=4.0)
    cfg = StudyConfig(seed=boot_seed, bootstrap_iters=400)
    report = run_study(cfg, frame=rows)
    coh = next(c for c in report.coefficients if c.name == "coherence_score")
    # Sign recovery
    assert coh.point > 0, f"expected positive coefficient, got {coh.point}"
    # 99% CI excludes zero ⇒ rejects H0 at alpha=0.01
    primary = report.primary_hypothesis_result
    assert primary["rejected_null"] is True
    assert primary["ci_lower"] > 0


def test_quintile_dose_response_is_monotonic_on_planted_effect() -> None:
    rows = _planted_frame(n=400, seed=7, beta_coh=4.0)
    cfg = StudyConfig(seed=99, bootstrap_iters=200)
    report = run_study(cfg, frame=rows)
    secondary = report.secondary_hypothesis_result
    assert secondary["monotonic_non_decreasing"] is True
    assert secondary["q5_minus_q1"] > 0.05
    assert secondary["rejected_null"] is True


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_markdown_renderer_mentions_headline_facts() -> None:
    rows = _planted_frame(n=240, seed=5)
    cfg = StudyConfig(seed=5, bootstrap_iters=200)
    report = run_study(cfg, frame=rows)
    md = vr.render_markdown(report.to_canonical_dict())
    # Mentions N, primary, AUC, Brier, calibration, disclosure.
    assert "Validation study report" in md
    assert "Primary hypothesis" in md
    assert "AUC" in md
    assert "Brier score" in md
    assert "Calibration curve" in md
    assert "Disclosure" in md
    # Inline N(known outcome).
    assert str(report.n_known_outcome) in md
