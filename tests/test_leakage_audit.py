"""Tests for the leakage audit + temporal split (prompt 45, Wave 12).

Covers the three load-bearing audit invariants:

* Membership: a holdout pitch present in *any* training artifact's
  ``training_pitch_ids`` is detected and surfaces as
  ``LEAKAGE_DETECTED``.
* Temporal split: the 2020 buffer year is excluded from both
  partitions; declared holdout pitches outside the post-buffer window
  are flagged as failures.
* Distribution shift: a sharp planted drift on a single feature
  trips the PSI ≥ 0.50 hard-error band.
* Clean case: a corpus with disjoint pre-/post-2020 partitions and
  matching marginals passes audit cleanly.
* Validation-study integration: ``run_study`` refuses to render when
  the audit fails (LeakageDetectedError raised before any output bytes
  are produced).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List

import pytest

from coherence_engine.server.fund.services import leakage_audit as la
from coherence_engine.server.fund.services import temporal_split as ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(pitch_id: str, year: int, **extras: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"pitch_id": pitch_id, "pitch_year": year}
    body.update(extras)
    return body


def _write_index(
    tmp_path: Path,
    *,
    artifacts: List[Dict[str, Any]],
) -> Path:
    p = tmp_path / "training_artifacts_index.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": "training-artifacts-index-v1",
                "training_artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------------------
# temporal_split
# ---------------------------------------------------------------------------


def test_temporal_split_excludes_buffer_year() -> None:
    corpus = [
        _row("a", 2017),
        _row("b", 2019),
        _row("c", 2020),  # buffer
        _row("d", 2021),
        _row("e", 2023),
    ]
    result = ts.split(corpus)
    train_ids = {r["pitch_id"] for r in result.train}
    holdout_ids = {r["pitch_id"] for r in result.holdout}
    buffer_ids = {r["pitch_id"] for r in result.buffer_excluded}
    assert train_ids == {"a", "b"}
    assert holdout_ids == {"d", "e"}
    assert buffer_ids == {"c"}


def test_temporal_split_rejects_inverted_window() -> None:
    with pytest.raises(ts.TemporalSplitError):
        ts.SplitConfig(
            train_end_year=2021, buffer_year=2020, holdout_start_year=2019
        )


def test_temporal_split_undated_rows() -> None:
    corpus = [_row("a", 2018), {"pitch_id": "x"}]  # missing pitch_year
    result = ts.split(corpus)
    assert {r["pitch_id"] for r in result.undated_excluded} == {"x"}
    assert {r["pitch_id"] for r in result.train} == {"a"}


# ---------------------------------------------------------------------------
# Artifact membership leakage
# ---------------------------------------------------------------------------


def test_audit_detects_holdout_pitch_in_training_set(tmp_path: Path) -> None:
    # Synthetic corpus: 'leaked' is dated 2022 (holdout) but listed as
    # part of the training set of an artifact.
    corpus = [
        _row("train_2018", 2018),
        _row("leaked", 2022),
        _row("clean_2023", 2023),
    ]
    idx = _write_index(
        tmp_path,
        artifacts=[
            {
                "artifact_id": "contradiction_direction_c_hat",
                "kind": "embedding_direction_vector",
                "training_pitch_ids": ["train_2018", "leaked"],
                "training_set_hash": "PENDING_FIRST_FIT",
            }
        ],
    )
    cfg = la.AuditConfig(
        corpus=tuple(corpus),
        training_artifacts_index_path=idx,
    )
    report = la.audit(cfg)
    assert report.passed is False
    assert any("artifact_membership" in s for s in report.failed_assertions)
    overlap = report.artifact_membership[0].overlapping_pitch_ids
    assert "leaked" in overlap
    with pytest.raises(la.LeakageDetectedError) as exc:
        la.enforce(report)
    assert la.LEAKAGE_DETECTED in str(exc.value)
    assert exc.value.code == la.LEAKAGE_DETECTED


def test_audit_clean_case_passes(tmp_path: Path) -> None:
    corpus = [
        _row(f"t{i:03d}", 2017 + (i % 3)) for i in range(20)
    ] + [_row(f"h{i:03d}", 2022 + (i % 2)) for i in range(20)]
    idx = _write_index(
        tmp_path,
        artifacts=[
            {
                "artifact_id": "contradiction_direction_c_hat",
                "kind": "embedding_direction_vector",
                "training_pitch_ids": [f"t{i:03d}" for i in range(20)],
                "training_set_hash": "PENDING_FIRST_FIT",
            }
        ],
    )
    cfg = la.AuditConfig(
        corpus=tuple(corpus),
        training_artifacts_index_path=idx,
    )
    report = la.audit(cfg)
    assert report.passed is True
    assert report.failed_assertions == ()
    assert report.audit_digest  # non-empty
    la.enforce(report)  # does not raise


# ---------------------------------------------------------------------------
# Distribution drift
# ---------------------------------------------------------------------------


def test_audit_detects_distribution_drift(tmp_path: Path) -> None:
    # Train scores cluster near 0.2; holdout scores cluster near 0.9.
    rng = random.Random(0)
    train = [
        _row(
            f"t{i:03d}",
            2018,
            coherence_score=0.2 + rng.gauss(0, 0.02),
        )
        for i in range(120)
    ]
    holdout = [
        _row(
            f"h{i:03d}",
            2023,
            coherence_score=0.9 + rng.gauss(0, 0.02),
        )
        for i in range(120)
    ]
    idx = _write_index(
        tmp_path,
        artifacts=[
            {
                "artifact_id": "contradiction_direction_c_hat",
                "kind": "embedding_direction_vector",
                "training_pitch_ids": [],
                "training_set_hash": "PENDING_FIRST_FIT",
            }
        ],
    )
    cfg = la.AuditConfig(
        corpus=tuple(train + holdout),
        feature_extractors=("coherence_score",),
        training_artifacts_index_path=idx,
    )
    report = la.audit(cfg)
    assert report.passed is False
    drift = next(d for d in report.feature_drift if d.feature == "coherence_score")
    assert drift.psi_alarm == "error"
    assert drift.psi >= 0.5
    assert drift.ks_alarm is True
    assert any("distribution_drift" in s for s in report.failed_assertions)


def test_audit_no_drift_when_marginals_match(tmp_path: Path) -> None:
    # Both partitions share the same uniform distribution.
    rng = random.Random(7)
    rows = []
    for i in range(120):
        rows.append(_row(f"t{i:03d}", 2017 + (i % 3), coherence_score=rng.random()))
    for i in range(120):
        rows.append(_row(f"h{i:03d}", 2022 + (i % 2), coherence_score=rng.random()))
    idx = _write_index(
        tmp_path,
        artifacts=[
            {
                "artifact_id": "contradiction_direction_c_hat",
                "kind": "embedding_direction_vector",
                "training_pitch_ids": [],
                "training_set_hash": "PENDING_FIRST_FIT",
            }
        ],
    )
    cfg = la.AuditConfig(
        corpus=tuple(rows),
        feature_extractors=("coherence_score",),
        training_artifacts_index_path=idx,
    )
    report = la.audit(cfg)
    assert report.passed is True


# ---------------------------------------------------------------------------
# Buffer-override guard
# ---------------------------------------------------------------------------


def test_audit_rejects_buffer_override_without_rationale(tmp_path: Path) -> None:
    idx = _write_index(tmp_path, artifacts=[])
    cfg = la.AuditConfig(
        corpus=(),
        training_artifacts_index_path=idx,
        train_end="2020-12-31",
        buffer_year=2021,
        holdout_start="2022-01-01",
    )
    report = la.audit(cfg)
    assert report.passed is False
    assert any("buffer_year override" in s for s in report.failed_assertions)


def test_audit_accepts_buffer_override_with_rationale(tmp_path: Path) -> None:
    idx = _write_index(tmp_path, artifacts=[])
    cfg = la.AuditConfig(
        corpus=(),
        training_artifacts_index_path=idx,
        train_end="2020-12-31",
        buffer_year=2021,
        holdout_start="2022-01-01",
        buffer_override_rationale="explicit operator approval; see YAML amendment v1.1",
    )
    report = la.audit(cfg)
    assert report.passed is True


# ---------------------------------------------------------------------------
# Declared-holdout outside post-buffer window
# ---------------------------------------------------------------------------


def test_audit_flags_declared_holdout_outside_window(tmp_path: Path) -> None:
    idx = _write_index(tmp_path, artifacts=[])
    corpus = [_row("pre", 2017), _row("post", 2022)]
    cfg = la.AuditConfig(
        corpus=tuple(corpus),
        # 'pre' is in the training window — declaring it as holdout is
        # an error caught by the audit.
        holdout_pitch_ids=("pre", "post"),
        training_artifacts_index_path=idx,
    )
    report = la.audit(cfg)
    assert report.passed is False
    assert "pre" in report.temporal_split.holdout_outside_window_pitch_ids


# ---------------------------------------------------------------------------
# KS / PSI sanity
# ---------------------------------------------------------------------------


def test_ks_two_sample_zero_for_identical_samples() -> None:
    a = [0.1, 0.2, 0.3, 0.4, 0.5]
    b = [0.1, 0.2, 0.3, 0.4, 0.5]
    stat, crit, alarm = la.ks_two_sample(a, b)
    assert stat == 0.0
    assert alarm is False


def test_ks_two_sample_alarms_on_disjoint_samples() -> None:
    # n=20 each: critical = 1.36 * sqrt(40/400) = 0.43; stat = 1.0 ⇒ alarm
    a = [0.0 + 0.01 * i for i in range(20)]
    b = [0.5 + 0.01 * i for i in range(20)]
    stat, crit, alarm = la.ks_two_sample(a, b)
    assert stat == pytest.approx(1.0)
    assert alarm is True


def test_psi_zero_on_identical_distributions() -> None:
    rng = random.Random(0)
    samples = [rng.random() for _ in range(500)]
    psi = la.population_stability_index(samples, list(samples))
    assert psi < 0.01


def test_psi_high_on_shifted_distributions() -> None:
    rng = random.Random(0)
    train = [rng.gauss(0, 1) for _ in range(500)]
    holdout = [rng.gauss(3, 1) for _ in range(500)]
    psi = la.population_stability_index(train, holdout)
    assert psi >= 0.5


# ---------------------------------------------------------------------------
# Validation-study integration: study refuses to render on audit failure
# ---------------------------------------------------------------------------


def test_validation_study_blocks_render_on_leakage(tmp_path: Path) -> None:
    """When the audit fails, run_study must raise before writing output."""

    from coherence_engine.server.fund.services.validation_study import (
        StudyConfig,
        StudyRow,
        run_study,
    )

    # Build a 240-row planted-effect frame so the stopping-rule check passes.
    rng = random.Random(0)
    rows = []
    for i in range(240):
        coh = rng.random()
        survived = 1 if rng.random() < (0.2 + 0.6 * coh) else 0
        pid = f"{i:08x}-0000-7000-8000-{i:012x}"
        rows.append(
            StudyRow(
                pitch_id=pid,
                domain="fintech",
                coherence_score=coh,
                check_size_usd=50_000.0,
                survival_5yr=survived,
            )
        )

    # Create a manifest that places one of the StudyRow pitches in the
    # holdout window AND lists it as a training pitch in the artifacts
    # index — a guaranteed leak.
    leaked_pid = rows[0].pitch_id
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"pitch_id": leaked_pid, "pitch_year": 2022}) + "\n",
        encoding="utf-8",
    )
    idx = _write_index(
        tmp_path,
        artifacts=[
            {
                "artifact_id": "contradiction_direction_c_hat",
                "kind": "embedding_direction_vector",
                "training_pitch_ids": [leaked_pid],
                "training_set_hash": "PENDING_FIRST_FIT",
            }
        ],
    )
    out = tmp_path / "report.json"
    cfg = StudyConfig(
        seed=0,
        bootstrap_iters=50,
        output_path=out,
        corpus_manifest_path=manifest,
        training_artifacts_index_path=idx,
    )
    with pytest.raises(la.LeakageDetectedError):
        run_study(cfg, frame=rows)
    # No output bytes should have been written.
    assert not out.exists()
