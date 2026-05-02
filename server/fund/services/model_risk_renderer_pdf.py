"""LaTeX → PDF rendering pipeline for the quarterly MRM report (prompt 60).

This module is the only place we shell out to ``pdflatex``. It owns:

* The Jinja2 environment configured for LaTeX (square-bracket
  delimiters, autoescaping disabled, custom escape filter for the
  handful of LaTeX special characters that appear in our payloads).
* :func:`render_tex` — pure function from ``MRMReportData`` to a
  ``.tex`` source string. Tests exercise this directly so the
  determinism contract does not depend on a pdflatex install.
* :func:`render_pdf` — compiles a ``.tex`` source via the
  ``pdflatex`` executable inside an isolated temp directory and
  returns the resulting PDF bytes plus the captured ``.log``. Two
  passes are run so the layout settles.

Why a fresh temp directory per render
-------------------------------------

pdflatex writes a small swarm of auxiliary files (``.aux``, ``.log``,
``.toc``, ``.out``) that it consults on the second pass. Running each
render in a fresh ``tempfile.TemporaryDirectory`` keeps concurrent
renders isolated and guarantees that the only file we hand back is
the actual PDF — everything else, including the log, is captured into
memory and the temp dir is removed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import jinja2

from coherence_engine.server.fund.services.model_risk_report import (
    DEFAULT_TEMPLATE_PATH,
    MRMReportData,
    MRMReportError,
)


_LOG = logging.getLogger(__name__)

PDFLATEX_EXECUTABLE = "pdflatex"
PDFLATEX_PASS_COUNT = 2
PDFLATEX_TIMEOUT_SECONDS = 60


class PdflatexNotInstalled(MRMReportError):
    """Raised when ``pdflatex`` is not on PATH.

    Surfacing this as a distinct exception lets the CLI print a
    helpful message ("install MacTeX / TeX Live") rather than dumping
    a ``FileNotFoundError`` traceback.
    """


class PdflatexRenderError(MRMReportError):
    """Raised when ``pdflatex`` returns non-zero.

    The ``.log`` content is attached as ``log_text`` so the caller
    can write it next to the failed run for debugging without having
    to reach into the temp dir (which we deliberately delete).
    """

    def __init__(self, message: str, *, log_text: str = "", returncode: int = 1):
        super().__init__(message)
        self.log_text = log_text
        self.returncode = returncode


@dataclass(frozen=True)
class PdfRenderResult:
    pdf_bytes: bytes
    log_text: str
    pages: int  # best-effort from the log; 0 when unparseable


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------


def _latex_escape(value: Any) -> str:
    """Escape the small set of LaTeX special characters we may emit.

    The payloads we render are governed (no user-supplied text without
    aggregation), so the escape set is intentionally narrow: backslash
    first, then the standard punctuation pile. Any future additions
    must keep ``\\`` first so the substitution does not double-escape
    its own output.
    """

    if value is None:
        return ""
    s = str(value)
    replacements = (
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    )
    for needle, repl in replacements:
        s = s.replace(needle, repl)
    return s


def _build_environment(template_dir: Path) -> jinja2.Environment:
    """Build a Jinja2 environment with LaTeX-friendly delimiters.

    Square-bracket delimiters keep ``{`` and ``}`` available for
    LaTeX literals; ``trim_blocks`` + ``lstrip_blocks`` keep the
    rendered ``.tex`` indentation predictable, which is load-bearing
    for the byte-determinism test.
    """

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        block_start_string="[%",
        block_end_string="%]",
        variable_start_string="[[",
        variable_end_string="]]",
        comment_start_string="[#",
        comment_end_string="#]",
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["latex"] = _latex_escape
    env.filters["pct"] = lambda v: f"{float(v) * 100.0:.2f}\\%"
    env.filters["fmt6"] = lambda v: f"{float(v):.6f}"
    env.filters["fmt2"] = lambda v: f"{float(v):.2f}"
    return env


# ---------------------------------------------------------------------------
# Template payload
# ---------------------------------------------------------------------------


def _to_template_context(data: MRMReportData) -> Dict[str, Any]:
    """Adapt ``MRMReportData`` into the Jinja2 context shape.

    The context shape is *flat* for sections and uses the same key
    names as the report payload so the template reads like a one-line
    field map. We expose ``has_*`` booleans so empty sections can be
    rendered as a "(no data this quarter)" line rather than disappearing.
    """

    return {
        "schema_version": data.schema_version,
        "quarter_label": data.quarter_label,
        "quarter_start": data.quarter_start,
        "quarter_end": data.quarter_end,
        "generated_at": data.generated_at,
        "disclaimer": data.disclaimer,
        "input_digest": data.input_digest,
        "model_purpose": data.model_purpose,
        "model_limitations": list(data.model_limitations),
        "has_model_purpose": bool(data.model_purpose),
        "has_limitations": bool(data.model_limitations),
        "has_validation": data.validation_summary is not None,
        "validation": (
            None
            if data.validation_summary is None
            else {
                "schema_version": data.validation_summary.schema_version,
                "n_known_outcome": data.validation_summary.n_known_outcome,
                "auc_roc": data.validation_summary.auc_roc,
                "brier_score": data.validation_summary.brier_score,
                "primary_rejected_null": data.validation_summary.primary_rejected_null,
                "primary_alpha": data.validation_summary.primary_alpha,
                "coherence_point_estimate": data.validation_summary.coherence_point_estimate,
                "coherence_ci_99_lower": data.validation_summary.coherence_ci_99_lower,
                "coherence_ci_99_upper": data.validation_summary.coherence_ci_99_upper,
                "data_hash": data.validation_summary.data_hash,
            }
        ),
        "has_drift": bool(data.drift_indicators),
        "drift_indicators": [
            {
                "metric": d.metric,
                "baseline_value": d.baseline_value,
                "current_value": d.current_value,
                "delta": d.delta,
                "threshold": d.threshold,
                "breached": d.breached,
            }
            for d in data.drift_indicators
        ],
        "has_overrides": bool(data.override_partner_stats),
        "override_total_count": data.override_total_count,
        "override_partner_stats": [
            {
                "partner_hash": s.partner_hash,
                "n_overrides": s.n_overrides,
                "n_pass_to_reject": s.n_pass_to_reject,
                "n_reject_to_pass": s.n_reject_to_pass,
                "most_common_reason_code": s.most_common_reason_code,
            }
            for s in data.override_partner_stats
        ],
        "has_anti_gaming": bool(data.anti_gaming_rates),
        "anti_gaming_rates": [
            {
                "period_label": r.period_label,
                "n_decisions": r.n_decisions,
                "n_alerts": r.n_alerts,
                "rate": r.rate,
            }
            for r in data.anti_gaming_rates
        ],
        "has_reproducibility": bool(data.reproducibility_audits),
        "reproducibility_audits": [
            {
                "audit_id": a.audit_id,
                "n_replays": a.n_replays,
                "n_matching": a.n_matching,
                "match_rate": a.match_rate,
                "notes": a.notes,
            }
            for a in data.reproducibility_audits
        ],
        "has_weaknesses": bool(data.known_weaknesses),
        "known_weaknesses": [
            {
                "item_id": b.item_id,
                "title": b.title,
                "severity": b.severity,
                "status": b.status,
                "owner": b.owner,
                "target_quarter": b.target_quarter,
            }
            for b in data.known_weaknesses
        ],
        "has_remediation": bool(data.remediation_backlog),
        "remediation_backlog": [
            {
                "item_id": b.item_id,
                "title": b.title,
                "severity": b.severity,
                "status": b.status,
                "owner": b.owner,
                "target_quarter": b.target_quarter,
            }
            for b in data.remediation_backlog
        ],
    }


# ---------------------------------------------------------------------------
# Public render entry points
# ---------------------------------------------------------------------------


def render_tex(
    data: MRMReportData,
    *,
    template_path: Optional[Path] = None,
) -> str:
    """Render a deterministic LaTeX source string from report data."""

    tpl_path = Path(template_path) if template_path else DEFAULT_TEMPLATE_PATH
    if not tpl_path.is_file():
        raise MRMReportError(f"template not found: {tpl_path}")
    env = _build_environment(tpl_path.parent)
    template = env.get_template(tpl_path.name)
    context = _to_template_context(data)
    rendered = template.render(**context)
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


def render_pdf(
    data: MRMReportData,
    *,
    template_path: Optional[Path] = None,
) -> PdfRenderResult:
    """Render the report and compile it through pdflatex.

    Two pdflatex passes are run; the first builds the ``.aux`` index,
    the second uses it. The pipeline is silent on success and surfaces
    the captured ``.log`` text on any failure so debug doesn't require
    fishing through a temp directory.
    """

    if shutil.which(PDFLATEX_EXECUTABLE) is None:
        raise PdflatexNotInstalled(
            "pdflatex executable not found on PATH — install MacTeX / TeX Live "
            "or run with --tex-only to skip PDF compilation."
        )

    tex_source = render_tex(data, template_path=template_path)

    with tempfile.TemporaryDirectory(prefix="mrm-report-") as tmp:
        work = Path(tmp)
        tex_path = work / "report.tex"
        tex_path.write_bytes(tex_source.encode("utf-8"))

        last_log = ""
        for pass_index in range(PDFLATEX_PASS_COUNT):
            try:
                proc = subprocess.run(
                    [
                        PDFLATEX_EXECUTABLE,
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-output-directory",
                        str(work),
                        str(tex_path),
                    ],
                    cwd=str(work),
                    capture_output=True,
                    timeout=PDFLATEX_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PdflatexRenderError(
                    f"pdflatex timed out after {PDFLATEX_TIMEOUT_SECONDS}s "
                    f"on pass {pass_index + 1}",
                    log_text=last_log,
                    returncode=124,
                ) from exc
            log_path = work / "report.log"
            if log_path.is_file():
                last_log = log_path.read_text(encoding="utf-8", errors="replace")
            else:
                last_log = (
                    proc.stdout.decode("utf-8", errors="replace")
                    + "\n"
                    + proc.stderr.decode("utf-8", errors="replace")
                )
            if proc.returncode != 0:
                raise PdflatexRenderError(
                    f"pdflatex failed on pass {pass_index + 1} with "
                    f"return code {proc.returncode}",
                    log_text=last_log,
                    returncode=proc.returncode,
                )

        pdf_path = work / "report.pdf"
        if not pdf_path.is_file():
            raise PdflatexRenderError(
                "pdflatex returned 0 but produced no PDF",
                log_text=last_log,
                returncode=0,
            )
        pdf_bytes = pdf_path.read_bytes()
        pages = _parse_page_count(last_log)
        return PdfRenderResult(pdf_bytes=pdf_bytes, log_text=last_log, pages=pages)


def _parse_page_count(log_text: str) -> int:
    """Best-effort scrape of the page count from a pdflatex log."""

    import re as _re

    m = _re.search(r"Output written on .*?\((\d+) pages?", log_text, _re.DOTALL)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


__all__ = [
    "PDFLATEX_EXECUTABLE",
    "PDFLATEX_PASS_COUNT",
    "PDFLATEX_TIMEOUT_SECONDS",
    "PdfRenderResult",
    "PdflatexNotInstalled",
    "PdflatexRenderError",
    "render_pdf",
    "render_tex",
]
