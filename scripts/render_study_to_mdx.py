"""Render a validation-study report (canonical JSON, prompt 44) into a
public-facing MDX page for the marketing site (apps/site).

The renderer is intentionally pure: it consumes the JSON payload and
emits MDX bytes. It never re-fits, never opens the database, and
never reaches out to the network. Identical input ⇒ identical MDX
bytes (deterministic).

Refusal contract (load-bearing):
  * If ``generated_with.leakage_audit_passed`` is not the literal
    string ``"true"``, :func:`render` raises
    :class:`PublicationRefused`. Prompt 45's leakage audit is the
    publication gate; this renderer cannot bypass it.

Headline contract (load-bearing):
  * When the primary hypothesis was NOT rejected (null or wrong-sign
    finding), the headline must NOT include any of
    ``successfully``, ``confirmed``, ``validated``. The renderer
    composes the headline from a fixed lookup so a copywriter cannot
    accidentally re-introduce spin.

Usage::

    python -m scripts.render_study_to_mdx \\
        --study-json data/governed/validation/study_v1.0.json \\
        --output-dir apps/site/src/content/results \\
        --feed-path  apps/site/public/results/feed.xml

The two output paths default to the locations above when omitted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
DEFAULT_OUTPUT_DIR = _REPO_ROOT / "apps" / "site" / "src" / "content" / "results"
DEFAULT_FEED_PATH = _REPO_ROOT / "apps" / "site" / "public" / "results" / "feed.xml"


SPIN_WORDS = ("successfully", "confirmed", "validated")


class PublicationRefused(RuntimeError):
    """Raised when the report does not satisfy the publication gate."""


# ---------------------------------------------------------------------------
# Frontmatter / slug
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_version(version: str) -> str:
    s = (version or "v0").strip().lower()
    s = _SLUG_RE.sub("_", s).strip("_")
    return s or "v0"


def _yaml_string(value: str) -> str:
    """Conservative YAML scalar quoting.

    The frontmatter is YAML, so any value containing a colon, quote,
    or non-ASCII character is wrapped in double quotes with the inner
    backslash / double-quote characters escaped. The output is stable
    given the same input.
    """

    text = "" if value is None else str(value)
    needs_quote = (
        text == ""
        or any(ch in text for ch in (":", "#", "'", '"', "{", "}", "[", "]", ",", "&", "*", "!", "|", ">", "%", "@", "`"))
        or text.strip() != text
        or text.lower() in {"true", "false", "yes", "no", "null", "~"}
    )
    if not needs_quote:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def headline_for(report: Mapping[str, Any]) -> str:
    """Return the page headline.

    Pinned phrasing — the renderer does not paraphrase. The two
    branches read identically aside from the actual finding so a
    reader can compare runs without parsing prose.
    """

    primary = report.get("primary_hypothesis_result") or {}
    rejected = bool(primary.get("rejected_null"))
    point = primary.get("point_estimate")
    try:
        point_f = float(point)
    except (TypeError, ValueError):
        point_f = 0.0
    n_known = int(report.get("n_known_outcome", 0) or 0)

    if rejected:
        return (
            f"Out-of-sample test rejected H0 at the pre-registered alpha "
            f"(N={n_known}, beta_coherence={point_f:+.3f}). Coherence "
            f"score carries non-trivial predictive information; magnitude "
            f"and calibration reported below."
        )
    # Negative / null finding. No spin words.
    return (
        f"Out-of-sample test did NOT reject H0 (N={n_known}, "
        f"beta_coherence={point_f:+.3f}). The coherence score's "
        f"predictive validity remains unproven on this cohort. Full "
        f"coefficient table and limitations below."
    )


def _domain_count(report: Mapping[str, Any]) -> int:
    domains = report.get("domain_breakdown") or {}
    if isinstance(domains, Mapping):
        return len(domains)
    return 0


def _published_at(report: Mapping[str, Any]) -> str:
    """Pick a date-like string from the report.

    Order of preference:
      1. ``preregistration.published_at`` (operator-injected)
      2. ``preregistration.amendments[-1].date``
      3. The pre-registration ``version`` (so the same study version
         always yields the same value).

    The renderer must be deterministic, so it never reads
    ``datetime.now`` or any environment variable.
    """

    prereg = report.get("preregistration") or {}
    if isinstance(prereg, Mapping):
        explicit = prereg.get("published_at")
        if isinstance(explicit, str) and explicit:
            return explicit
        amendments = prereg.get("amendments") or []
        if isinstance(amendments, list) and amendments:
            last = amendments[-1]
            if isinstance(last, Mapping):
                d = last.get("date") or last.get("on")
                if isinstance(d, str) and d:
                    return d
        version = prereg.get("version")
        if isinstance(version, str) and version:
            return version
    return "unknown"


def build_frontmatter(report: Mapping[str, Any]) -> Dict[str, Any]:
    prereg = report.get("preregistration") or {}
    name = prereg.get("study_name") or "validation-study"
    version = prereg.get("version") or "v0"
    return {
        "title": f"Validation study — {name} ({version})",
        "published_at": _published_at(report),
        "version": version,
        "n_pitches": int(report.get("n_known_outcome", 0) or 0),
        "domain_count": _domain_count(report),
        "headline": headline_for(report),
        "rejected_null": bool(
            (report.get("primary_hypothesis_result") or {}).get("rejected_null")
        ),
        "data_hash": str(report.get("data_hash") or ""),
        "leakage_audit_digest": str(
            (report.get("generated_with") or {}).get("leakage_audit_digest", "")
        ),
        "schema_version": str(report.get("schema_version") or ""),
    }


def render_frontmatter_yaml(fm: Mapping[str, Any]) -> str:
    """Render the frontmatter dict as a stable YAML block.

    Keys are emitted in a fixed order (not alphabetical — the
    presentation order matters for human readers) and all string
    values go through :func:`_yaml_string` so two runs of the same
    report yield byte-identical bytes.
    """

    order = [
        "title",
        "published_at",
        "version",
        "n_pitches",
        "domain_count",
        "headline",
        "rejected_null",
        "data_hash",
        "leakage_audit_digest",
        "schema_version",
    ]
    lines = ["---"]
    for k in order:
        v = fm.get(k)
        if isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {_yaml_string(v if v is not None else '')}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SVG plot helpers (pure-stdlib, deterministic)
# ---------------------------------------------------------------------------


def _fmt_num(value: Any, *, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _svg_open(width: int, height: int, label: str) -> List[str]:
    return [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
            f'{width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="{label}">'
        ),
    ]


def svg_coefficient_intervals(
    coefficients: Sequence[Mapping[str, Any]],
    *,
    width: int = 640,
    row_height: int = 28,
    margin: int = 90,
) -> str:
    """Forest-plot SVG of point estimates with 95% CIs.

    The x-axis range is computed from the coefficient bounds; if the
    range degenerates (single point) we widen it to a fixed slack so
    the resulting graphic is still readable.
    """

    coefs = list(coefficients)
    n = len(coefs)
    height = max(120, margin + row_height * max(1, n) + 40)
    plot_left = margin
    plot_right = width - 16
    plot_width = max(40, plot_right - plot_left)

    lows = []
    highs = []
    for c in coefs:
        try:
            lows.append(float(c.get("ci_lower_95", 0.0)))
            highs.append(float(c.get("ci_upper_95", 0.0)))
            lows.append(float(c.get("point", 0.0)))
            highs.append(float(c.get("point", 0.0)))
        except (TypeError, ValueError):
            continue
    if not lows:
        lows = [-1.0]
        highs = [1.0]
    lo = min(lows)
    hi = max(highs)
    if hi - lo < 1e-6:
        lo -= 0.5
        hi += 0.5
    pad = 0.05 * (hi - lo)
    lo -= pad
    hi += pad

    def x_of(v: float) -> float:
        return plot_left + (v - lo) / (hi - lo) * plot_width

    parts = _svg_open(width, height, "Coefficient point estimates with 95% confidence intervals")
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
    )
    # zero line
    if lo <= 0 <= hi:
        zx = x_of(0.0)
        parts.append(
            f'<line x1="{zx:.2f}" y1="20" x2="{zx:.2f}" y2="{height - 30}" '
            f'stroke="#999" stroke-dasharray="2,3"/>'
        )
    # axis ticks: lo, 0, hi
    ticks = sorted({round(lo, 4), 0.0, round(hi, 4)})
    for t in ticks:
        if not (lo <= t <= hi):
            continue
        tx = x_of(t)
        parts.append(
            f'<line x1="{tx:.2f}" y1="{height - 30}" x2="{tx:.2f}" '
            f'y2="{height - 24}" stroke="#444"/>'
        )
        parts.append(
            f'<text x="{tx:.2f}" y="{height - 10}" font-size="11" '
            f'text-anchor="middle" fill="#444">{_fmt_num(t, digits=2)}</text>'
        )

    for i, c in enumerate(coefs):
        y = 30 + i * row_height
        name = str(c.get("name", ""))
        try:
            point = float(c.get("point", 0.0))
            lo95 = float(c.get("ci_lower_95", 0.0))
            hi95 = float(c.get("ci_upper_95", 0.0))
        except (TypeError, ValueError):
            continue
        parts.append(
            f'<text x="{plot_left - 8:.2f}" y="{y + 4}" font-size="11" '
            f'text-anchor="end" fill="#222">{name}</text>'
        )
        parts.append(
            f'<line x1="{x_of(lo95):.2f}" y1="{y}" x2="{x_of(hi95):.2f}" '
            f'y2="{y}" stroke="#1f6feb" stroke-width="2"/>'
        )
        parts.append(
            f'<line x1="{x_of(lo95):.2f}" y1="{y - 4}" x2="{x_of(lo95):.2f}" '
            f'y2="{y + 4}" stroke="#1f6feb" stroke-width="2"/>'
        )
        parts.append(
            f'<line x1="{x_of(hi95):.2f}" y1="{y - 4}" x2="{x_of(hi95):.2f}" '
            f'y2="{y + 4}" stroke="#1f6feb" stroke-width="2"/>'
        )
        parts.append(
            f'<circle cx="{x_of(point):.2f}" cy="{y}" r="4" fill="#1f6feb"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_calibration_curve(
    bins: Sequence[Mapping[str, Any]],
    *,
    width: int = 480,
    height: int = 360,
    margin: int = 48,
) -> str:
    """Reliability diagram: realized vs predicted, with diagonal."""

    plot_left = margin
    plot_right = width - 12
    plot_top = 16
    plot_bottom = height - margin
    plot_w = plot_right - plot_left
    plot_h = plot_bottom - plot_top

    def x_of(v: float) -> float:
        return plot_left + max(0.0, min(1.0, v)) * plot_w

    def y_of(v: float) -> float:
        return plot_bottom - max(0.0, min(1.0, v)) * plot_h

    parts = _svg_open(width, height, "Calibration curve: predicted vs realized")
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
    )
    # axes
    parts.append(
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_right}" '
        f'y2="{plot_bottom}" stroke="#444"/>'
    )
    parts.append(
        f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" '
        f'y2="{plot_bottom}" stroke="#444"/>'
    )
    # diagonal reference
    parts.append(
        f'<line x1="{x_of(0):.2f}" y1="{y_of(0):.2f}" x2="{x_of(1):.2f}" '
        f'y2="{y_of(1):.2f}" stroke="#999" stroke-dasharray="3,3"/>'
    )
    # ticks at 0, 0.25, 0.5, 0.75, 1
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        tx = x_of(t)
        ty = y_of(t)
        parts.append(
            f'<line x1="{tx:.2f}" y1="{plot_bottom}" x2="{tx:.2f}" '
            f'y2="{plot_bottom + 4}" stroke="#444"/>'
        )
        parts.append(
            f'<text x="{tx:.2f}" y="{plot_bottom + 16}" font-size="10" '
            f'text-anchor="middle" fill="#444">{t:.2f}</text>'
        )
        parts.append(
            f'<line x1="{plot_left - 4}" y1="{ty:.2f}" x2="{plot_left}" '
            f'y2="{ty:.2f}" stroke="#444"/>'
        )
        parts.append(
            f'<text x="{plot_left - 6}" y="{ty + 3:.2f}" font-size="10" '
            f'text-anchor="end" fill="#444">{t:.2f}</text>'
        )
    parts.append(
        f'<text x="{plot_left + plot_w / 2:.2f}" y="{height - 8}" '
        f'font-size="11" text-anchor="middle" fill="#222">Predicted '
        f'probability</text>'
    )
    parts.append(
        f'<text x="14" y="{plot_top + plot_h / 2:.2f}" font-size="11" '
        f'text-anchor="middle" fill="#222" '
        f'transform="rotate(-90 14 {plot_top + plot_h / 2:.2f})">Realized '
        f'rate</text>'
    )
    # points + connecting line
    pts: List[Tuple[float, float, int]] = []
    for b in bins:
        try:
            count = int(b.get("count", 0) or 0)
            if count <= 0:
                continue
            mp = float(b.get("mean_predicted", 0.0))
            mr = float(b.get("mean_realized", 0.0))
        except (TypeError, ValueError):
            continue
        pts.append((x_of(mp), y_of(mr), count))
    if len(pts) >= 2:
        path_d = " ".join(
            (f"{'M' if i == 0 else 'L'}{p[0]:.2f},{p[1]:.2f}")
            for i, p in enumerate(pts)
        )
        parts.append(
            f'<path d="{path_d}" stroke="#1f6feb" stroke-width="1.5" '
            f'fill="none"/>'
        )
    for px, py, count in pts:
        r = 3.0 + min(8.0, count ** 0.5 / 2.0)
        parts.append(
            f'<circle cx="{px:.2f}" cy="{py:.2f}" r="{r:.2f}" '
            f'fill="#1f6feb" fill-opacity="0.7"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_per_domain_coefficients(
    domain_breakdown: Mapping[str, Mapping[str, Any]],
    *,
    width: int = 640,
    row_height: int = 28,
    margin: int = 130,
) -> str:
    """Forest-plot SVG of per-domain beta_coherence with 95% CIs."""

    items = sorted(domain_breakdown.items()) if domain_breakdown else []
    n = len(items)
    if n == 0:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 60" '
            'width="640" height="60" role="img" aria-label="Per-domain '
            'coefficients (no domain met the minimum N)"><rect width="640" '
            'height="60" fill="#fafafa"/><text x="320" y="36" font-size="12" '
            'text-anchor="middle" fill="#666">no domain met the minimum '
            'sample size</text></svg>'
        )
    height = max(120, 30 + row_height * n + 40)
    plot_left = margin
    plot_right = width - 16
    plot_w = plot_right - plot_left

    lows: List[float] = []
    highs: List[float] = []
    for _d, info in items:
        try:
            lows.append(float(info.get("ci_95_lower", 0.0)))
            highs.append(float(info.get("ci_95_upper", 0.0)))
            lows.append(float(info.get("beta_coherence", 0.0)))
            highs.append(float(info.get("beta_coherence", 0.0)))
        except (TypeError, ValueError):
            continue
    if not lows:
        lows, highs = [-1.0], [1.0]
    lo = min(lows)
    hi = max(highs)
    if hi - lo < 1e-6:
        lo -= 0.5
        hi += 0.5
    pad = 0.05 * (hi - lo)
    lo -= pad
    hi += pad

    def x_of(v: float) -> float:
        return plot_left + (v - lo) / (hi - lo) * plot_w

    parts = _svg_open(width, height, "Per-domain coherence_score coefficients (95% CIs)")
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
    )
    if lo <= 0 <= hi:
        zx = x_of(0.0)
        parts.append(
            f'<line x1="{zx:.2f}" y1="20" x2="{zx:.2f}" y2="{height - 30}" '
            f'stroke="#999" stroke-dasharray="2,3"/>'
        )
    for tk in sorted({round(lo, 4), 0.0, round(hi, 4)}):
        if not (lo <= tk <= hi):
            continue
        tx = x_of(tk)
        parts.append(
            f'<text x="{tx:.2f}" y="{height - 10}" font-size="11" '
            f'text-anchor="middle" fill="#444">{_fmt_num(tk, digits=2)}</text>'
        )
    for i, (d, info) in enumerate(items):
        y = 30 + i * row_height
        try:
            point = float(info.get("beta_coherence", 0.0))
            lo95 = float(info.get("ci_95_lower", 0.0))
            hi95 = float(info.get("ci_95_upper", 0.0))
        except (TypeError, ValueError):
            continue
        nrows = info.get("n", "")
        label = f"{d} (N={nrows})"
        parts.append(
            f'<text x="{plot_left - 8:.2f}" y="{y + 4}" font-size="11" '
            f'text-anchor="end" fill="#222">{label}</text>'
        )
        parts.append(
            f'<line x1="{x_of(lo95):.2f}" y1="{y}" x2="{x_of(hi95):.2f}" '
            f'y2="{y}" stroke="#1f6feb" stroke-width="2"/>'
        )
        parts.append(
            f'<circle cx="{x_of(point):.2f}" cy="{y}" r="4" fill="#1f6feb"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Body sections
# ---------------------------------------------------------------------------


def _section_design(report: Mapping[str, Any]) -> List[str]:
    prereg = report.get("preregistration") or {}
    primary = prereg.get("primary_hypothesis") or {}
    bootstrap = prereg.get("bootstrap") or {}
    out = ["## Design", ""]
    out.append(
        "This study is a pre-registered, out-of-sample test. The "
        "hypothesis, alpha levels, sample-size stopping rule, and "
        "bootstrap procedure were all locked into "
        "`data/governed/validation/preregistration.yaml` before any "
        "outcome data was unblinded."
    )
    out.append("")
    out.append(f"- **Test:** `{primary.get('test', 'n/a')}`")
    out.append(
        f"- **Parameter of interest:** `{primary.get('parameter_of_interest', 'n/a')}`"
    )
    out.append(f"- **Direction:** {primary.get('direction', 'n/a')}")
    out.append(f"- **Pre-registered alpha:** {primary.get('alpha', 'n/a')}")
    out.append(f"- **Bootstrap iterations:** {bootstrap.get('iterations', 'n/a')}")
    out.append(f"- **Resample unit:** `{bootstrap.get('resample_unit', 'n/a')}`")
    out.append("")
    return out


def _section_sample(report: Mapping[str, Any]) -> List[str]:
    out = ["## Sample", ""]
    out.append(f"- **N (total joined rows):** {report.get('n_total', 0)}")
    out.append(f"- **N (known outcome, used in fit):** {report.get('n_known_outcome', 0)}")
    out.append(f"- **N (excluded as unknown):** {report.get('n_excluded_unknown', 0)}")
    out.append(f"- **Domains observed:** {_domain_count(report)}")
    insufficient = report.get("insufficient_subgroups") or []
    if insufficient:
        out.append(
            "- **Insufficient subgroups (N < per-domain minimum):** "
            + ", ".join(f"`{d}`" for d in insufficient)
        )
    out.append(f"- **Data hash:** `{report.get('data_hash', '')}`")
    out.append("")
    return out


def _coef_table(coefs: Sequence[Mapping[str, Any]]) -> List[str]:
    out: List[str] = []
    out.append("| Term | Point | 95% CI | 99% CI |")
    out.append("|---|---|---|---|")
    for c in coefs:
        ci95 = (
            f"[{_fmt_num(c.get('ci_lower_95'))}, "
            f"{_fmt_num(c.get('ci_upper_95'))}]"
        )
        ci99 = (
            f"[{_fmt_num(c.get('ci_lower_99'))}, "
            f"{_fmt_num(c.get('ci_upper_99'))}]"
        )
        out.append(
            f"| `{c.get('name')}` | {_fmt_num(c.get('point'))} | {ci95} | {ci99} |"
        )
    return out


def _section_results(report: Mapping[str, Any]) -> List[str]:
    coefs = list(report.get("coefficients") or [])
    metrics = report.get("metrics") or {}
    primary = report.get("primary_hypothesis_result") or {}
    secondary = report.get("secondary_hypothesis_result") or {}
    bins = list(report.get("calibration_curve") or [])
    domain_break = report.get("domain_breakdown") or {}

    out = ["## Results", ""]
    out.append("### Primary hypothesis (H1)")
    out.append("")
    ci_label = primary.get("ci_used", "ci_99")
    out.append(f"- **Pre-registered alpha:** {primary.get('alpha')}")
    out.append(
        f"- **Coefficient on `coherence_score`:** "
        f"{_fmt_num(primary.get('point_estimate'))}"
    )
    out.append(
        f"- **{ci_label}:** [{_fmt_num(primary.get('ci_lower'))}, "
        f"{_fmt_num(primary.get('ci_upper'))}]"
    )
    out.append(f"- **Excludes zero:** {primary.get('excludes_zero')}")
    out.append(
        f"- **Direction consistent with pre-registration:** "
        f"{primary.get('direction_consistent')}"
    )
    out.append(f"- **Reject H0:** {primary.get('rejected_null')}")
    out.append("")

    out.append("### Coefficient table")
    out.append("")
    out.extend(_coef_table(coefs))
    out.append("")
    out.append("<figure>")
    out.append(svg_coefficient_intervals(coefs))
    out.append("<figcaption>Point estimates with 95% bootstrap CIs.</figcaption>")
    out.append("</figure>")
    out.append("")

    out.append("### Predictive metrics")
    out.append("")
    out.append(f"- **AUC (ROC):** {_fmt_num(metrics.get('auc_roc'))}")
    out.append(f"- **Brier score:** {_fmt_num(metrics.get('brier_score'))}")
    out.append(
        f"- **Mean predicted probability:** "
        f"{_fmt_num(metrics.get('mean_predicted_probability'))}"
    )
    out.append(
        f"- **Realized positive rate:** "
        f"{_fmt_num(metrics.get('realized_positive_rate'))}"
    )
    out.append(f"- **Convergence:** {metrics.get('convergence', 'n/a')}")
    out.append("")

    out.append("### Calibration")
    out.append("")
    out.append("<figure>")
    out.append(svg_calibration_curve(bins))
    out.append(
        "<figcaption>Realized rate vs mean predicted probability per bin. "
        "Marker area is proportional to bin count; the dashed line is "
        "perfect calibration.</figcaption>"
    )
    out.append("</figure>")
    out.append("")

    out.append("### Secondary hypothesis (H2 — quintile dose-response)")
    out.append("")
    rates = secondary.get("quintile_rates") or []
    counts = secondary.get("quintile_counts") or []
    if rates:
        out.append("| Quintile | N | Realized rate |")
        out.append("|---|---|---|")
        for i, r in enumerate(rates):
            n = counts[i] if i < len(counts) else 0
            out.append(f"| Q{i + 1} | {n} | {_fmt_num(r)} |")
        out.append("")
    out.append(f"- **Q5 − Q1:** {_fmt_num(secondary.get('q5_minus_q1'))}")
    out.append(
        f"- **Monotonic non-decreasing:** "
        f"{secondary.get('monotonic_non_decreasing')}"
    )
    out.append(f"- **Reject H0 (H2):** {secondary.get('rejected_null')}")
    out.append("")

    out.append("### Per-domain sub-models (Bonferroni-corrected)")
    out.append("")
    if domain_break:
        out.append(
            "| Domain | N | beta_coh | 95% CI | corrected CI | Reject H0 |"
        )
        out.append("|---|---|---|---|---|---|")
        for d in sorted(domain_break.keys()):
            info = domain_break[d] or {}
            ci95 = (
                f"[{_fmt_num(info.get('ci_95_lower'))}, "
                f"{_fmt_num(info.get('ci_95_upper'))}]"
            )
            ci_corr = (
                f"[{_fmt_num(info.get('ci_corrected_lower'))}, "
                f"{_fmt_num(info.get('ci_corrected_upper'))}]"
            )
            out.append(
                f"| `{d}` | {info.get('n')} | "
                f"{_fmt_num(info.get('beta_coherence'))} | {ci95} | "
                f"{ci_corr} | {info.get('rejected_null_corrected')} |"
            )
        out.append("")
    out.append("<figure>")
    out.append(svg_per_domain_coefficients(domain_break))
    out.append(
        "<figcaption>Per-domain beta_coherence with 95% CIs. Corrected "
        "CIs use a Bonferroni divisor of k where k is the number of "
        "domains that met the minimum sample size.</figcaption>"
    )
    out.append("</figure>")
    out.append("")
    return out


def _section_interpretation(report: Mapping[str, Any]) -> List[str]:
    primary = report.get("primary_hypothesis_result") or {}
    secondary = report.get("secondary_hypothesis_result") or {}
    rejected_h1 = bool(primary.get("rejected_null"))
    rejected_h2 = bool(secondary.get("rejected_null"))
    out = ["## Interpretation", ""]
    if rejected_h1:
        out.append(
            "The pre-registered primary test rejected H0: the bootstrap "
            "confidence interval on `beta_coherence` excludes zero in "
            "the pre-registered direction. This is evidence that the "
            "coherence score carries non-trivial predictive information "
            "for the outcome on this cohort, AFTER controlling for "
            "domain and check size. It is NOT evidence of causation — "
            "see the scope-boundary block in the pre-registration."
        )
    else:
        out.append(
            "The pre-registered primary test did NOT reject H0. On this "
            "cohort, the bootstrap confidence interval on "
            "`beta_coherence` does not exclude zero in the "
            "pre-registered direction. The coherence score's predictive "
            "validity remains unproven; we report the result with the "
            "same prominence as a positive finding by policy."
        )
    out.append("")
    if rejected_h2:
        out.append(
            "The secondary dose-response test (H2) shows a monotonic "
            "non-decreasing realized survival rate across coherence "
            "quintiles, with a Q5−Q1 gap above the pre-registered "
            "threshold."
        )
    else:
        out.append(
            "The secondary dose-response test (H2) did not reach the "
            "pre-registered threshold. Quintile-level rates are reported "
            "above without smoothing."
        )
    out.append("")
    return out


def _section_limitations(report: Mapping[str, Any]) -> List[str]:
    primary = report.get("primary_hypothesis_result") or {}
    rejected = bool(primary.get("rejected_null"))
    n_known = int(report.get("n_known_outcome", 0) or 0)
    insufficient = list(report.get("insufficient_subgroups") or [])
    out = ["## Limitations", ""]
    if not rejected:
        out.append(
            "This was a null finding. Two distinct mechanisms can "
            "produce a null at this sample size: (1) the score truly "
            "carries no predictive information on this population, or "
            "(2) the study was underpowered. We report both alongside "
            "the observed CI so a reader can decide which is more "
            "consistent with the data. The decision to publish this "
            "negative result is documented in the pre-registration's "
            "`negative_results_policy` block."
        )
    out.append(
        f"- N is {n_known} known-outcome rows. Power to detect a small "
        "true effect at the pre-registered alpha is bounded by N; we "
        "do not currently publish a post-hoc power curve."
    )
    if insufficient:
        out.append(
            "- The following domains did not meet the per-domain "
            "minimum sample size and are excluded from the Bonferroni "
            "family: " + ", ".join(f"`{d}`" for d in insufficient) + "."
        )
    out.append(
        "- The fit is logistic regression on three engineered features "
        "(`coherence_score`, `domain_primary`, `log(check_size_usd)`). "
        "Non-linear interactions are not modeled."
    )
    out.append(
        "- The leakage audit (prompt 45) passed before this report was "
        "rendered (its digest is in the page frontmatter), but it is "
        "an audit of *measured* leakage paths only; novel leakage "
        "mechanisms not in its checklist would not be caught."
    )
    out.append("")
    return out


def _section_links(report: Mapping[str, Any]) -> List[str]:
    config = report.get("config") or {}
    prereg_path = config.get("preregistration_path") or ""
    raw_path = config.get("output_path") or ""
    out = ["## Links", ""]
    if prereg_path:
        out.append(
            f"- Pre-registration document: `{prereg_path}` "
            "(immutable; amendments require a version bump)"
        )
    if raw_path:
        out.append(f"- Raw study report (canonical JSON): `{raw_path}`")
    out.append(
        "- Renderer: `scripts/render_study_to_mdx.py` (deterministic; "
        "same JSON ⇒ same MDX bytes)"
    )
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------


def _enforce_publication_gate(report: Mapping[str, Any]) -> None:
    gen = report.get("generated_with") or {}
    passed = str(gen.get("leakage_audit_passed", ""))
    if passed != "true":
        raise PublicationRefused(
            "leakage audit did not pass — refusing to publish. "
            f"generated_with.leakage_audit_passed={passed!r}. "
            "See docs/specs/leakage_audit.md."
        )


def render(report: Mapping[str, Any]) -> str:
    """Render the canonical study JSON to a complete MDX document.

    The function is pure: same input dict ⇒ same output string.
    """

    if not isinstance(report, Mapping):
        raise TypeError("render() requires a mapping (canonical study JSON)")
    _enforce_publication_gate(report)

    fm = build_frontmatter(report)
    parts: List[str] = [render_frontmatter_yaml(fm), ""]
    parts.append(f"# {fm['title']}")
    parts.append("")
    parts.append(f"_{fm['headline']}_")
    parts.append("")
    parts.extend(_section_design(report))
    parts.extend(_section_sample(report))
    parts.extend(_section_results(report))
    parts.extend(_section_interpretation(report))
    parts.extend(_section_limitations(report))
    parts.extend(_section_links(report))
    body = "\n".join(parts)
    if not body.endswith("\n"):
        body += "\n"
    return body


def slug_for(report: Mapping[str, Any]) -> str:
    prereg = report.get("preregistration") or {}
    version = prereg.get("version") or "v0"
    return f"study_{_slugify_version(str(version))}"


def write_mdx(
    report: Mapping[str, Any],
    *,
    output_dir: Optional[os.PathLike[str] | str] = None,
) -> Path:
    """Render and write the MDX file. Returns the output path."""

    target_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{slug_for(report)}.mdx"
    out.write_text(render(report), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_feed_xml(reports: Sequence[Mapping[str, Any]], *, site_url: str) -> str:
    """Build a minimal RSS 2.0 feed across study reports.

    The output is deterministic: items are sorted by ``published_at``
    descending, then by version, so the same set of reports always
    produces the same bytes.
    """

    site = site_url.rstrip("/")
    items_data: List[Tuple[str, str, str, Mapping[str, Any]]] = []
    for r in reports:
        fm = build_frontmatter(r)
        items_data.append(
            (str(fm["published_at"]), str(fm["version"]), str(fm["title"]), fm)
        )
    items_data.sort(key=lambda x: (x[0], x[1]), reverse=True)

    parts: List[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<rss version="2.0">')
    parts.append("  <channel>")
    parts.append("    <title>Coherence Engine — Validation results</title>")
    parts.append(f"    <link>{_xml_escape(site)}/results/</link>")
    parts.append(
        "    <description>Pre-registered out-of-sample validation studies. "
        "Negative findings are published with the same prominence as "
        "positive ones.</description>"
    )
    parts.append("    <language>en-us</language>")
    for pub, version, title, fm in items_data:
        slug = f"study_{_slugify_version(version)}"
        link = f"{site}/results/{slug}/"
        parts.append("    <item>")
        parts.append(f"      <title>{_xml_escape(title)}</title>")
        parts.append(f"      <link>{_xml_escape(link)}</link>")
        parts.append(f"      <guid isPermaLink=\"true\">{_xml_escape(link)}</guid>")
        parts.append(
            f"      <description>{_xml_escape(str(fm['headline']))}</description>"
        )
        parts.append(f"      <pubDate>{_xml_escape(pub)}</pubDate>")
        parts.append("    </item>")
    parts.append("  </channel>")
    parts.append("</rss>")
    return "\n".join(parts) + "\n"


def write_feed(
    reports: Sequence[Mapping[str, Any]],
    *,
    feed_path: Optional[os.PathLike[str] | str] = None,
    site_url: str = "https://coherence.example.com",
) -> Path:
    target = Path(feed_path) if feed_path else DEFAULT_FEED_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_feed_xml(reports, site_url=site_url), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_reports_from_dir(dirpath: Path) -> List[Mapping[str, Any]]:
    reports: List[Mapping[str, Any]] = []
    if not dirpath.is_dir():
        return reports
    for p in sorted(dirpath.glob("*.json")):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            reports.append(payload)
    return reports


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render a validation-study canonical JSON to a public MDX page "
            "and refresh the RSS feed across all rendered studies."
        )
    )
    parser.add_argument(
        "--study-json",
        type=Path,
        required=True,
        help="Path to a single study report JSON to render.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write the MDX into (apps/site/src/content/results).",
    )
    parser.add_argument(
        "--feed-path",
        type=Path,
        default=DEFAULT_FEED_PATH,
        help="Path of the feed.xml to (re)write across all studies.",
    )
    parser.add_argument(
        "--studies-dir",
        type=Path,
        default=None,
        help=(
            "Directory of additional study JSONs to include in feed.xml. "
            "Defaults to the directory of --study-json."
        ),
    )
    parser.add_argument(
        "--site-url",
        type=str,
        default="https://coherence.example.com",
        help="Site URL used to build absolute links in feed.xml.",
    )
    args = parser.parse_args(argv)

    payload = json.loads(args.study_json.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        print("ERROR: study JSON must be a JSON object", file=sys.stderr)
        return 2
    try:
        out = write_mdx(payload, output_dir=args.output_dir)
    except PublicationRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    studies_dir = args.studies_dir or args.study_json.parent
    reports = _load_reports_from_dir(studies_dir)
    if not reports:
        reports = [payload]
    write_feed(reports, feed_path=args.feed_path, site_url=args.site_url)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
