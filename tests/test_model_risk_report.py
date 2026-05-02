"""Tests for the quarterly Model-Risk-Management report (prompt 60).

Covers:

* :func:`assemble_quarterly_report` produces a deterministic
  ``MRMReportData`` (same inputs → same canonical bytes / digest).
* The LaTeX renderer is byte-deterministic: the same ``MRMReportData``
  yields identical ``.tex`` source bytes — the cheapest signal of
  accidental drift in template ordering or assembler aggregation.
* Missing source files produce empty sections rather than aborting
  (the report should be runnable even before every source has data).
* ``QuarterRef.parse`` rejects malformed quarter strings.
* Backlog YAML loader parses the canonical seed file.
* PDF generation (``render_pdf``) succeeds on the assembled report
  when ``pdflatex`` is on PATH; the test is *skipped* otherwise but
  emits a loud warning so a CI environment without TeX does not
  silently lose coverage.
"""

from __future__ import annotations

import json
import shutil
import warnings
from pathlib import Path

import pytest

from coherence_engine.server.fund.services import model_risk_report as mrm
from coherence_engine.server.fund.services import model_risk_renderer_pdf as mrm_pdf


_FIXED_GENERATED_AT = "2026-04-25T12:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_validation_study(path: Path) -> None:
    payload = {
        "schema_version": "validation-study-report-v1",
        "n_known_outcome": 1234,
        "data_hash": "deadbeef" * 8,
        "metrics": {
            "auc_roc": 0.71234,
            "brier_score": 0.18765,
        },
        "primary_hypothesis_result": {
            "rejected_null": True,
            "alpha": 0.01,
        },
        "coefficients": [
            {
                "name": "intercept",
                "point": -1.2,
                "ci_lower_99": -1.5,
                "ci_upper_99": -0.9,
            },
            {
                "name": "coherence_score",
                "point": 3.41,
                "ci_lower_99": 2.10,
                "ci_upper_99": 4.71,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_drift_telemetry(path: Path) -> None:
    payload = {
        "indicators": [
            {
                "metric": "auc_roc",
                "baseline_value": 0.72,
                "current_value": 0.69,
                "threshold": 0.05,
            },
            {
                "metric": "brier_score",
                "baseline_value": 0.19,
                "current_value": 0.21,
                "threshold": 0.03,
            },
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_override_stats(path: Path) -> None:
    payload = {
        "total_overrides": 17,
        "by_partner": [
            {
                "partner_id": "partner-alpha",
                "n_overrides": 9,
                "n_pass_to_reject": 3,
                "n_reject_to_pass": 1,
                "most_common_reason_code": "factual_error",
            },
            {
                "partner_id": "partner-beta",
                "n_overrides": 8,
                "n_pass_to_reject": 2,
                "n_reject_to_pass": 4,
                "most_common_reason_code": "manual_diligence",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_anti_gaming_stats(path: Path) -> None:
    payload = {
        "series": [
            {"period_label": "2026-04", "n_decisions": 200, "n_alerts": 7},
            {"period_label": "2026-05", "n_decisions": 180, "n_alerts": 5},
            {"period_label": "2026-06", "n_decisions": 210, "n_alerts": 9},
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_repro_audit(path: Path) -> None:
    payload = {
        "audits": [
            {
                "audit_id": "repro-2026-04",
                "n_replays": 50,
                "n_matching": 50,
                "notes": "All replays bit-identical.",
            },
            {
                "audit_id": "repro-2026-05",
                "n_replays": 50,
                "n_matching": 49,
                "notes": "One drift: pinned floating-point delta.",
            },
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_inputs(tmp_path: Path) -> mrm.MRMReportInputs:
    vs = tmp_path / "validation.json"
    drift = tmp_path / "drift.json"
    over = tmp_path / "over.json"
    ag = tmp_path / "ag.json"
    repro = tmp_path / "repro.json"
    _write_validation_study(vs)
    _write_drift_telemetry(drift)
    _write_override_stats(over)
    _write_anti_gaming_stats(ag)
    _write_repro_audit(repro)
    return mrm.MRMReportInputs(
        quarter=mrm.QuarterRef(year=2026, quarter=2),
        generated_at=_FIXED_GENERATED_AT,
        validation_study_path=vs,
        drift_telemetry_path=drift,
        override_stats_path=over,
        anti_gaming_alert_stats_path=ag,
        reproducibility_audit_path=repro,
        backlog_path=mrm.DEFAULT_BACKLOG_PATH,
    )


# ---------------------------------------------------------------------------
# QuarterRef
# ---------------------------------------------------------------------------


def test_quarter_ref_parse_round_trip() -> None:
    q = mrm.QuarterRef.parse("2026Q2")
    assert q.year == 2026
    assert q.quarter == 2
    assert q.label == "2026Q2"
    start, end = q.covers_iso_dates()
    assert start == "2026-04-01"
    assert end == "2026-06-30"


@pytest.mark.parametrize("bad", ["", "Q2", "2026", "2026-Q2", "2026Q5", "2026Q0", "20Q2"])
def test_quarter_ref_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(mrm.MRMReportError):
        mrm.QuarterRef.parse(bad)


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


def test_assemble_quarterly_report_runs_with_seed_backlog(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    data = mrm.assemble_quarterly_report(inputs)
    assert data.schema_version == mrm.MRM_REPORT_SCHEMA_VERSION
    assert data.quarter_label == "2026Q2"
    assert data.disclaimer == mrm.MRM_DISCLAIMER
    assert "informed by" in data.disclaimer.lower()
    assert "compliant" in data.disclaimer.lower()
    assert data.validation_summary is not None
    assert data.validation_summary.n_known_outcome == 1234
    assert data.validation_summary.coherence_point_estimate == pytest.approx(3.41)
    assert len(data.drift_indicators) == 2
    metrics = {d.metric for d in data.drift_indicators}
    assert metrics == {"auc_roc", "brier_score"}
    assert data.override_total_count == 17
    assert len(data.override_partner_stats) == 2
    # partner ids are hashed, not raw
    for s in data.override_partner_stats:
        assert "partner-" not in s.partner_hash
        assert len(s.partner_hash) == 12
    assert {r.period_label for r in data.anti_gaming_rates} == {
        "2026-04",
        "2026-05",
        "2026-06",
    }
    # backlog seed contributes both weakness and remediation items
    assert len(data.known_weaknesses) >= 1
    assert len(data.remediation_backlog) >= 1
    # severity ordering: critical/high before medium/low
    severities = [b.severity for b in data.known_weaknesses]
    assert severities == sorted(
        severities, key=lambda s: mrm._severity_rank(s)
    )


def test_assemble_quarterly_report_deterministic_canonical_bytes(
    tmp_path: Path,
) -> None:
    """Two assembly runs with identical inputs produce identical bytes."""

    inputs = _make_inputs(tmp_path)
    data_a = mrm.assemble_quarterly_report(inputs)
    data_b = mrm.assemble_quarterly_report(inputs)
    assert data_a.to_canonical_bytes() == data_b.to_canonical_bytes()
    assert data_a.report_digest() == data_b.report_digest()
    assert data_a.input_digest == data_b.input_digest


def test_assemble_quarterly_report_handles_missing_sources(tmp_path: Path) -> None:
    """Empty sources do not abort the report."""

    inputs = mrm.MRMReportInputs(
        quarter=mrm.QuarterRef(year=2026, quarter=2),
        generated_at=_FIXED_GENERATED_AT,
        backlog_path=tmp_path / "no-such-backlog.yaml",
    )
    data = mrm.assemble_quarterly_report(inputs)
    assert data.validation_summary is None
    assert data.drift_indicators == ()
    assert data.override_partner_stats == ()
    assert data.anti_gaming_rates == ()
    assert data.reproducibility_audits == ()
    assert data.known_weaknesses == ()
    assert data.remediation_backlog == ()
    assert data.input_digest  # still computed


def test_assemble_rejects_non_quarterref() -> None:
    with pytest.raises(mrm.MRMReportError):
        mrm.assemble_quarterly_report(
            mrm.MRMReportInputs(  # type: ignore[arg-type]
                quarter="2026Q2",
                generated_at=_FIXED_GENERATED_AT,
            )
        )


# ---------------------------------------------------------------------------
# Renderer (.tex determinism)
# ---------------------------------------------------------------------------


def test_render_tex_is_deterministic(tmp_path: Path) -> None:
    """Same MRMReportData → byte-identical .tex source.

    This is the cheapest signal of accidental drift in template
    ordering or in the assembler's aggregation logic, and is the test
    the prompt explicitly forbids removing.
    """

    inputs = _make_inputs(tmp_path)
    data = mrm.assemble_quarterly_report(inputs)
    tex_a = mrm_pdf.render_tex(data)
    tex_b = mrm_pdf.render_tex(data)
    assert tex_a == tex_b
    assert tex_a.encode("utf-8") == tex_b.encode("utf-8")


def test_render_tex_contains_disclaimer_and_quarter(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    data = mrm.assemble_quarterly_report(inputs)
    tex = mrm_pdf.render_tex(data)
    assert "MRM" in tex or "Model-Risk-Management" in tex
    assert "2026Q2" in tex
    assert "informed by" in tex.lower()
    # critical LaTeX preamble pieces from the prompt
    assert "\\documentclass" in tex
    assert "lmodern" in tex
    assert "microtype" in tex


def test_render_tex_escapes_special_characters(tmp_path: Path) -> None:
    """LaTeX special characters in payload values must be escaped."""

    bad = mrm._latex_escape if hasattr(mrm, "_latex_escape") else None
    _ = bad
    # Drive escaping via a backlog item with awkward characters.
    backlog = tmp_path / "backlog.yaml"
    backlog.write_text(
        "model_purpose: \"Test purpose\"\n"
        "model_limitations:\n"
        "  - description: \"Watch the & and % and _ characters\"\n"
        "known_weaknesses: []\n"
        "remediation_backlog:\n"
        "  - item_id: RB-X\n"
        "    title: \"Fix 100% of bugs\"\n"
        "    severity: low\n"
        "    status: open\n"
        "    owner: tooling\n"
        "    target_quarter: 2026Q3\n",
        encoding="utf-8",
    )
    inputs = mrm.MRMReportInputs(
        quarter=mrm.QuarterRef(year=2026, quarter=2),
        generated_at=_FIXED_GENERATED_AT,
        backlog_path=backlog,
    )
    data = mrm.assemble_quarterly_report(inputs)
    tex = mrm_pdf.render_tex(data)
    # The literal "&", "%", "_" must NOT appear unescaped inside the
    # rendered limitation text. We assert the escaped forms are present.
    assert r"\&" in tex
    assert r"\%" in tex
    assert r"\_" in tex


# ---------------------------------------------------------------------------
# PDF generation (skipped if pdflatex absent)
# ---------------------------------------------------------------------------


_PDFLATEX_AVAILABLE = shutil.which(mrm_pdf.PDFLATEX_EXECUTABLE) is not None


@pytest.mark.skipif(
    not _PDFLATEX_AVAILABLE,
    reason=(
        "pdflatex not on PATH — skipping PDF-render coverage. "
        "Install MacTeX or TeX Live to run this test."
    ),
)
def test_render_pdf_succeeds_on_synthetic_fixture(tmp_path: Path) -> None:
    inputs = _make_inputs(tmp_path)
    data = mrm.assemble_quarterly_report(inputs)
    result = mrm_pdf.render_pdf(data)
    assert isinstance(result.pdf_bytes, bytes)
    assert result.pdf_bytes.startswith(b"%PDF-")
    assert result.pages >= 1
    assert "Output written on" in result.log_text


def test_render_pdf_raises_when_pdflatex_missing(monkeypatch, tmp_path: Path) -> None:
    """When pdflatex is absent, the renderer raises a typed error.

    We force the missing-binary path by monkey-patching ``shutil.which``
    so the test runs even on CI machines that *do* have pdflatex.
    """

    inputs = _make_inputs(tmp_path)
    data = mrm.assemble_quarterly_report(inputs)
    monkeypatch.setattr(mrm_pdf.shutil, "which", lambda _name: None)
    with pytest.raises(mrm_pdf.PdflatexNotInstalled):
        mrm_pdf.render_pdf(data)


# Loud warning at import-time so a CI run with no pdflatex doesn't
# silently lose coverage. The skipif above is the formal mechanism;
# this warning is the human-visible nudge.
if not _PDFLATEX_AVAILABLE:  # pragma: no cover - environment-dependent
    warnings.warn(
        "pdflatex not on PATH; "
        "test_render_pdf_succeeds_on_synthetic_fixture will be skipped. "
        "Install MacTeX / TeX Live to exercise the full PDF pipeline.",
        stacklevel=1,
    )
