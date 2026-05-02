"""Markdown renderer for the coherence-vs-outcome validation study report.

Reads the canonical JSON produced by
:mod:`coherence_engine.server.fund.services.validation_study` and emits a
human-readable Markdown brief. The renderer is pure-stdlib and never
re-fits anything — it is a pure function of the report payload, so the
output is deterministic for a given input file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Mapping


def _fmt_float(v: Any, *, digits: int = 4) -> str:
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _section_header(report: Mapping[str, Any]) -> List[str]:
    prereg = report.get("preregistration") or {}
    name = prereg.get("study_name", "validation-study")
    version = prereg.get("version", "")
    n_total = report.get("n_total", 0)
    n_known = report.get("n_known_outcome", 0)
    n_excluded = report.get("n_excluded_unknown", 0)
    data_hash = report.get("data_hash", "")
    return [
        f"# Validation study report — {name}",
        "",
        f"- **Pre-registration:** {version}",
        f"- **Schema:** `{report.get('schema_version','')}`",
        f"- **N(total joined rows):** {n_total}",
        f"- **N(known outcome, used):** {n_known}",
        f"- **N(excluded as unknown):** {n_excluded}",
        f"- **Data hash:** `{data_hash}`",
        "",
    ]


def _section_primary(report: Mapping[str, Any]) -> List[str]:
    primary = report.get("primary_hypothesis_result") or {}
    out = ["## Primary hypothesis (H1)", ""]
    if not primary:
        out.append("_(no primary result emitted)_")
        out.append("")
        return out
    out.append(f"- **Alpha:** {primary.get('alpha')}")
    out.append(
        f"- **Coefficient on coherence_score:** {_fmt_float(primary.get('point_estimate'))}"
    )
    ci_label = primary.get("ci_used", "ci_99")
    out.append(
        f"- **{ci_label}:** [{_fmt_float(primary.get('ci_lower'))}, "
        f"{_fmt_float(primary.get('ci_upper'))}]"
    )
    out.append(f"- **Excludes zero:** {primary.get('excludes_zero')}")
    out.append(f"- **Direction consistent:** {primary.get('direction_consistent')}")
    out.append(f"- **Reject H0:** {primary.get('rejected_null')}")
    out.append("")
    return out


def _section_secondary(report: Mapping[str, Any]) -> List[str]:
    secondary = report.get("secondary_hypothesis_result") or {}
    out = ["## Secondary hypothesis (H2 — quintile dose-response)", ""]
    if not secondary:
        out.append("_(no secondary result emitted)_")
        out.append("")
        return out
    rates = secondary.get("quintile_rates") or []
    counts = secondary.get("quintile_counts") or []
    out.append("| Quintile | N | Realized rate |")
    out.append("|---|---|---|")
    for i, r in enumerate(rates):
        n = counts[i] if i < len(counts) else 0
        out.append(f"| Q{i+1} | {n} | {_fmt_float(r)} |")
    out.append("")
    out.append(
        f"- **Q5 - Q1:** {_fmt_float(secondary.get('q5_minus_q1'))}"
    )
    out.append(
        f"- **Monotonic non-decreasing:** {secondary.get('monotonic_non_decreasing')}"
    )
    out.append(f"- **Reject H0:** {secondary.get('rejected_null')}")
    out.append("")
    return out


def _section_metrics(report: Mapping[str, Any]) -> List[str]:
    metrics = report.get("metrics") or {}
    out = ["## Predictive metrics", ""]
    out.append(f"- **AUC (ROC):** {_fmt_float(metrics.get('auc_roc'))}")
    out.append(f"- **Brier score:** {_fmt_float(metrics.get('brier_score'))}")
    out.append(
        f"- **Mean predicted prob.:** {_fmt_float(metrics.get('mean_predicted_probability'))}"
    )
    out.append(
        f"- **Realized positive rate:** {_fmt_float(metrics.get('realized_positive_rate'))}"
    )
    out.append(f"- **Convergence:** {metrics.get('convergence', 'n/a')}")
    out.append("")
    return out


def _section_coefficients(report: Mapping[str, Any]) -> List[str]:
    coefs = report.get("coefficients") or []
    out = ["## Coefficient table (with bootstrap CIs)", ""]
    out.append("| Term | Point | 95% CI | 99% CI |")
    out.append("|---|---|---|---|")
    for c in coefs:
        ci95 = (
            f"[{_fmt_float(c.get('ci_lower_95'))}, {_fmt_float(c.get('ci_upper_95'))}]"
        )
        ci99 = (
            f"[{_fmt_float(c.get('ci_lower_99'))}, {_fmt_float(c.get('ci_upper_99'))}]"
        )
        out.append(f"| `{c.get('name')}` | {_fmt_float(c.get('point'))} | {ci95} | {ci99} |")
    out.append("")
    return out


def _section_calibration(report: Mapping[str, Any]) -> List[str]:
    bins = report.get("calibration_curve") or []
    out = ["## Calibration curve", ""]
    out.append("| Bin | Range | N | Mean predicted | Mean realized |")
    out.append("|---|---|---|---|---|")
    for b in bins:
        rng = (
            f"[{_fmt_float(b.get('bin_lower'),digits=2)}, "
            f"{_fmt_float(b.get('bin_upper'),digits=2)})"
        )
        out.append(
            f"| {b.get('bin_index')} | {rng} | {b.get('count')} "
            f"| {_fmt_float(b.get('mean_predicted'))} "
            f"| {_fmt_float(b.get('mean_realized'))} |"
        )
    out.append("")
    return out


def _section_per_domain(report: Mapping[str, Any]) -> List[str]:
    domain = report.get("domain_breakdown") or {}
    insufficient = report.get("insufficient_subgroups") or []
    out = ["## Per-domain sub-models (Bonferroni-corrected)", ""]
    if not domain:
        out.append("_(no domain reached the per-domain minimum N.)_")
    else:
        out.append("| Domain | N | beta_coh | 95% CI | corrected CI | Reject H0 |")
        out.append("|---|---|---|---|---|---|")
        for d, info in domain.items():
            ci95 = (
                f"[{_fmt_float(info.get('ci_95_lower'))}, "
                f"{_fmt_float(info.get('ci_95_upper'))}]"
            )
            ci_corr = (
                f"[{_fmt_float(info.get('ci_corrected_lower'))}, "
                f"{_fmt_float(info.get('ci_corrected_upper'))}]"
            )
            out.append(
                f"| `{d}` | {info.get('n')} "
                f"| {_fmt_float(info.get('beta_coherence'))} "
                f"| {ci95} | {ci_corr} | {info.get('rejected_null_corrected')} |"
            )
    if insufficient:
        out.append("")
        out.append(
            "_Insufficient subgroups (N < per-domain minimum):_ "
            + ", ".join(f"`{d}`" for d in insufficient)
        )
    out.append("")
    return out


def _section_disclosure(report: Mapping[str, Any]) -> List[str]:
    prereg = report.get("preregistration") or {}
    scope = prereg.get("scope_boundary") or {}
    neg = prereg.get("negative_results_policy") or {}
    out = ["## Disclosure", ""]
    out.append(
        f"- **Claim kind:** {scope.get('claim_kind', 'prediction')} "
        f"(NOT {scope.get('not_claim_kind', 'causation')})"
    )
    if scope.get("notes"):
        out.append(f"- {scope['notes']}")
    out.append(
        f"- **Negative-results policy:** publish_when_null="
        f"{neg.get('publish_when_null')}, "
        f"publish_when_wrong_sign={neg.get('publish_when_wrong_sign')}."
    )
    out.append("")
    return out


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a complete Markdown report from the canonical JSON payload."""

    parts: List[str] = []
    parts.extend(_section_header(report))
    parts.extend(_section_primary(report))
    parts.extend(_section_secondary(report))
    parts.extend(_section_metrics(report))
    parts.extend(_section_coefficients(report))
    parts.extend(_section_calibration(report))
    parts.extend(_section_per_domain(report))
    parts.extend(_section_disclosure(report))
    return "\n".join(parts)


def render_from_file(path: os.PathLike[str] | str) -> str:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"validation report file is not a JSON object: {p}")
    return render_markdown(payload)


__all__ = ["render_from_file", "render_markdown"]
