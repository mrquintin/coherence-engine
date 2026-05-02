"""Tests for the public-results MDX renderer (prompt 46).

Covers:

* Determinism: same study JSON ⇒ byte-identical MDX (and the writer
  is idempotent on rerun).
* Negative-result template: when H1 was not rejected, the headline
  contains none of the prohibited spin words ("successfully",
  "confirmed", "validated").
* Publication gate: a report whose ``leakage_audit_passed`` is not
  the literal "true" is refused (no MDX is written).
* Frontmatter shape: required keys present, slug derived from
  pre-registration version, headline mirrors the rejected/null
  branch.
* RSS feed: entries are sorted by ``published_at`` desc and the feed
  is byte-deterministic.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader (the renderer lives at coherence_engine/scripts/, which is
# not a Python package — no __init__.py — so we import it by file path.)
# ---------------------------------------------------------------------------


def _load_renderer():
    here = Path(__file__).resolve()
    repo_root = here.parents[1]
    src = repo_root / "scripts" / "render_study_to_mdx.py"
    spec = importlib.util.spec_from_file_location("render_study_to_mdx", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_study_to_mdx"] = mod
    spec.loader.exec_module(mod)
    return mod


renderer = _load_renderer()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SPIN_WORDS = ("successfully", "confirmed", "validated")


def _base_report(*, rejected: bool) -> dict:
    """A canonical-shaped study report dict, deterministic by construction."""

    point = 1.42 if rejected else 0.18
    ci_lo_99 = 0.51 if rejected else -0.32
    ci_hi_99 = 2.31 if rejected else 0.71
    return {
        "schema_version": "validation-study-report-v1",
        "generated_with": {
            "numpy": "unavailable",
            "validation_study_schema": "validation-study-report-v1",
            "leakage_audit_digest": "deadbeef" * 8,
            "leakage_audit_passed": "true",
        },
        "config": {
            "preregistration_path": "/abs/preregistration.yaml",
            "output_path": "/abs/study_v1.0.json",
            "seed": 7,
            "bootstrap_iters": 200,
        },
        "preregistration": {
            "version": "v1.0",
            "study_name": "coherence-vs-survival-5yr-regression-v1",
            "primary_hypothesis": {
                "test": "logit(survival_5yr) ~ coherence_score + domain + log_check",
                "parameter_of_interest": "coherence_score",
                "direction": "positive",
                "alpha": 0.01,
            },
            "bootstrap": {"iterations": 10000, "resample_unit": "row"},
            "negative_results_policy": {
                "publish_when_null": True,
                "publish_when_wrong_sign": True,
            },
            "scope_boundary": {
                "claim_kind": "prediction",
                "not_claim_kind": "causation",
            },
            "amendments": [],
            "published_at": "2026-04-25",
        },
        "n_total": 280,
        "n_known_outcome": 240,
        "n_excluded_unknown": 40,
        "coefficients": [
            {
                "name": "intercept",
                "point": -0.5,
                "ci_lower_95": -0.9,
                "ci_upper_95": -0.1,
                "ci_lower_99": -1.1,
                "ci_upper_99": 0.1,
            },
            {
                "name": "coherence_score",
                "point": point,
                "ci_lower_95": ci_lo_99 + 0.2,
                "ci_upper_95": ci_hi_99 - 0.2,
                "ci_lower_99": ci_lo_99,
                "ci_upper_99": ci_hi_99,
            },
            {
                "name": "log_check_size",
                "point": 0.05,
                "ci_lower_95": -0.02,
                "ci_upper_95": 0.12,
                "ci_lower_99": -0.05,
                "ci_upper_99": 0.15,
            },
        ],
        "primary_hypothesis_result": {
            "alpha": 0.01,
            "ci_used": "ci_99",
            "ci_lower": ci_lo_99,
            "ci_upper": ci_hi_99,
            "point_estimate": point,
            "excludes_zero": rejected,
            "direction_consistent": rejected,
            "rejected_null": rejected,
        },
        "secondary_hypothesis_result": {
            "n": 240,
            "quintile_rates": [0.10, 0.15, 0.20, 0.30, 0.45] if rejected else [0.20, 0.18, 0.22, 0.21, 0.23],
            "quintile_counts": [48, 48, 48, 48, 48],
            "monotonic_non_decreasing": rejected,
            "q5_minus_q1": 0.35 if rejected else 0.03,
            "rejected_null": rejected,
        },
        "metrics": {
            "auc_roc": 0.71 if rejected else 0.52,
            "brier_score": 0.18,
            "convergence": "converged",
            "mean_predicted_probability": 0.42,
            "realized_positive_rate": 0.40,
        },
        "calibration_curve": [
            {
                "bin_index": k,
                "bin_lower": round(k / 10, 6),
                "bin_upper": round((k + 1) / 10, 6),
                "count": 24 if k % 2 == 0 else 0,
                "mean_predicted": round(k / 10 + 0.05, 6) if k % 2 == 0 else 0.0,
                "mean_realized": round(k / 10 + (0.05 if rejected else 0.02), 6) if k % 2 == 0 else 0.0,
            }
            for k in range(10)
        ],
        "domain_breakdown": {
            "fintech": {
                "n": 70,
                "converged": True,
                "beta_coherence": point + 0.1,
                "ci_95_lower": (ci_lo_99 + 0.2) - 0.05,
                "ci_95_upper": (ci_hi_99 - 0.2) + 0.05,
                "alpha_bonferroni": 0.025,
                "ci_corrected_lower": ci_lo_99,
                "ci_corrected_upper": ci_hi_99,
                "rejected_null_corrected": rejected,
            },
            "healthtech": {
                "n": 60,
                "converged": True,
                "beta_coherence": point - 0.1,
                "ci_95_lower": (ci_lo_99 + 0.2) - 0.10,
                "ci_95_upper": (ci_hi_99 - 0.2) + 0.10,
                "alpha_bonferroni": 0.025,
                "ci_corrected_lower": ci_lo_99 - 0.05,
                "ci_corrected_upper": ci_hi_99 + 0.05,
                "rejected_null_corrected": rejected,
            },
        },
        "insufficient_subgroups": ["consumer", "deeptech"],
        "data_hash": "f" * 64,
    }


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_render_is_deterministic() -> None:
    report = _base_report(rejected=True)
    a = renderer.render(report)
    b = renderer.render(deepcopy(report))
    assert a == b
    assert a.encode("utf-8") == b.encode("utf-8")


def test_write_mdx_is_byte_deterministic(tmp_path: Path) -> None:
    report = _base_report(rejected=True)
    p1 = renderer.write_mdx(report, output_dir=tmp_path / "a")
    p2 = renderer.write_mdx(report, output_dir=tmp_path / "b")
    assert p1.read_bytes() == p2.read_bytes()
    # And rerunning into the same dir is idempotent.
    p3 = renderer.write_mdx(report, output_dir=tmp_path / "a")
    assert p3 == p1
    assert p1.read_bytes() == p3.read_bytes()


# ---------------------------------------------------------------------------
# Negative-result language
# ---------------------------------------------------------------------------


def test_negative_result_headline_has_no_spin() -> None:
    report = _base_report(rejected=False)
    fm = renderer.build_frontmatter(report)
    headline = str(fm["headline"]).lower()
    for word in SPIN_WORDS:
        assert word not in headline, f"spin word '{word}' found in null-finding headline"


def test_negative_result_mdx_does_not_lead_with_spin() -> None:
    report = _base_report(rejected=False)
    _rendered = renderer.render(report)
    # The headline appears as the first emphasised paragraph after the title.
    # Find it and assert no spin word in it.
    headline_line = renderer.headline_for(report).lower()
    for word in SPIN_WORDS:
        assert word not in headline_line


def test_positive_result_headline_is_factual_not_celebratory() -> None:
    report = _base_report(rejected=True)
    headline = renderer.headline_for(report).lower()
    # Even on a positive result we don't use celebratory language.
    for word in SPIN_WORDS:
        assert word not in headline


# ---------------------------------------------------------------------------
# Publication gate
# ---------------------------------------------------------------------------


def test_render_refuses_when_leakage_audit_did_not_pass() -> None:
    report = _base_report(rejected=True)
    report["generated_with"]["leakage_audit_passed"] = "false"
    with pytest.raises(renderer.PublicationRefused):
        renderer.render(report)


def test_render_refuses_when_leakage_audit_field_missing() -> None:
    report = _base_report(rejected=True)
    report["generated_with"].pop("leakage_audit_passed", None)
    with pytest.raises(renderer.PublicationRefused):
        renderer.render(report)


def test_write_mdx_does_not_create_file_when_refused(tmp_path: Path) -> None:
    report = _base_report(rejected=True)
    report["generated_with"]["leakage_audit_passed"] = "false"
    out_dir = tmp_path / "results"
    with pytest.raises(renderer.PublicationRefused):
        renderer.write_mdx(report, output_dir=out_dir)
    # The directory may exist (mkdir runs first) but no MDX must be in it.
    assert not list(out_dir.glob("*.mdx"))


# ---------------------------------------------------------------------------
# Frontmatter shape and slug
# ---------------------------------------------------------------------------


def test_frontmatter_contains_required_keys() -> None:
    report = _base_report(rejected=True)
    fm = renderer.build_frontmatter(report)
    for key in (
        "title",
        "published_at",
        "version",
        "n_pitches",
        "domain_count",
        "headline",
    ):
        assert key in fm
    assert fm["version"] == "v1.0"
    assert fm["n_pitches"] == 240
    assert fm["domain_count"] == 2
    assert fm["published_at"] == "2026-04-25"


def test_slug_derives_from_version() -> None:
    report = _base_report(rejected=True)
    assert renderer.slug_for(report) == "study_v1_0"
    report["preregistration"]["version"] = "V2.3-beta"
    assert renderer.slug_for(report) == "study_v2_3_beta"


def test_mdx_starts_with_frontmatter_block() -> None:
    report = _base_report(rejected=True)
    out = renderer.render(report)
    assert out.startswith("---\n")
    end = out.find("\n---\n", 4)
    assert end > 0, "frontmatter close fence missing"
    fm_block = out[4:end]
    assert "title:" in fm_block
    assert "published_at:" in fm_block
    assert "version:" in fm_block
    assert "n_pitches:" in fm_block
    assert "domain_count:" in fm_block
    assert "headline:" in fm_block


def test_body_contains_required_sections() -> None:
    report = _base_report(rejected=True)
    out = renderer.render(report)
    for heading in (
        "## Design",
        "## Sample",
        "## Results",
        "## Interpretation",
        "## Limitations",
        "## Links",
    ):
        assert heading in out, f"missing section: {heading}"


def test_body_contains_embedded_svg_plots() -> None:
    report = _base_report(rejected=True)
    out = renderer.render(report)
    # Three plots: coefficient CIs, calibration, per-domain.
    assert out.count("<svg") >= 3
    assert "Calibration" in out
    assert "Per-domain" in out


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------


def test_render_feed_is_deterministic_and_sorted() -> None:
    r1 = _base_report(rejected=True)
    r1["preregistration"]["version"] = "v1.0"
    r1["preregistration"]["published_at"] = "2026-04-01"
    r2 = _base_report(rejected=False)
    r2["preregistration"]["version"] = "v1.1"
    r2["preregistration"]["published_at"] = "2026-04-25"

    feed_a = renderer.render_feed_xml([r1, r2], site_url="https://example.com")
    feed_b = renderer.render_feed_xml([r2, r1], site_url="https://example.com")
    assert feed_a == feed_b
    # The newer study is listed first.
    pos_v11 = feed_a.find("study_v1_1")
    pos_v10 = feed_a.find("study_v1_0")
    assert 0 <= pos_v11 < pos_v10


def test_write_feed_creates_valid_xml(tmp_path: Path) -> None:
    feed_path = tmp_path / "feed.xml"
    renderer.write_feed(
        [_base_report(rejected=False)],
        feed_path=feed_path,
        site_url="https://example.com",
    )
    text = feed_path.read_text(encoding="utf-8")
    assert text.startswith("<?xml")
    assert "<rss" in text
    assert "</rss>" in text
    # The single item links into /results/<slug>/.
    assert "https://example.com/results/study_v1_0/" in text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_writes_mdx_and_feed(tmp_path: Path) -> None:
    report = _base_report(rejected=True)
    study_path = tmp_path / "study_v1.0.json"
    study_path.write_text(json.dumps(report), encoding="utf-8")
    out_dir = tmp_path / "content_results"
    feed_path = tmp_path / "feed.xml"
    rc = renderer.main(
        [
            "--study-json",
            str(study_path),
            "--output-dir",
            str(out_dir),
            "--feed-path",
            str(feed_path),
            "--site-url",
            "https://example.com",
        ]
    )
    assert rc == 0
    files = list(out_dir.glob("*.mdx"))
    assert len(files) == 1
    assert files[0].name == "study_v1_0.mdx"
    assert feed_path.exists()


def test_cli_exits_nonzero_when_audit_did_not_pass(tmp_path: Path) -> None:
    report = _base_report(rejected=True)
    report["generated_with"]["leakage_audit_passed"] = "false"
    study_path = tmp_path / "study.json"
    study_path.write_text(json.dumps(report), encoding="utf-8")
    rc = renderer.main(
        [
            "--study-json",
            str(study_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--feed-path",
            str(tmp_path / "feed.xml"),
        ]
    )
    assert rc == 2
