"""LP reporting orchestration (prompt 69).

Assembles per-LP quarterly NAV statements by gluing together:

* :mod:`nav_calculator` (the math layer, pure-functional);
* a Jinja2 / LaTeX rendering pipeline that mirrors the model-risk
  renderer (square-bracket delimiters, ``StrictUndefined``,
  byte-deterministic output); and
* a sealed :class:`QuarterlyStatement` dataclass plus content digest
  the LP portal serves and the audit log references.

Two prompt-69 prohibitions are enforced here:

1. *No statement without signed Marks* — :func:`assemble_quarterly_statement`
   delegates the gate to :func:`nav_calculator.compute_nav`, which
   raises :class:`UnsignedMarkError`. The caller MUST surface the
   exception; this module never falls back to mark-to-cost.
2. *No cross-LP leakage* — every public function takes a single
   ``commitment`` argument and returns a single LP's statement. The
   batch helper :func:`assemble_batch` is a thin loop that yields one
   statement per LP without sharing intermediate state across LPs.

Determinism contract
--------------------

For a fixed input set (commitment, positions, marks, cash flows,
template path) the rendered ``.tex`` source bytes are identical
across runs. The :attr:`QuarterlyStatement.content_digest` is the
SHA-256 of the rendered ``.tex`` source and is the canonical
fingerprint used by the LP portal and the audit table.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import jinja2

from coherence_engine.server.fund.services.nav_calculator import (
    CashFlow,
    LPCommitment,
    Mark,
    NAVSnapshot,
    PortfolioPosition,
    compute_nav,
)


__all__ = [
    "QuarterlyStatement",
    "LPReportingError",
    "TemplateNotFoundError",
    "DEFAULT_QUARTERLY_TEMPLATE",
    "DEFAULT_CAPITAL_CALL_TEMPLATE",
    "DEFAULT_DISTRIBUTION_TEMPLATE",
    "TEMPLATE_DIR",
    "assemble_quarterly_statement",
    "assemble_batch",
    "render_quarterly_tex",
    "build_jinja_environment",
    "latex_escape",
    "compute_content_digest",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Template locations
# ---------------------------------------------------------------------------


# Resolved relative to repo root (``coherence_engine/``); all three LP
# templates ship under ``data/governed/lp_reports/templates/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR = _REPO_ROOT / "data" / "governed" / "lp_reports" / "templates"

DEFAULT_QUARTERLY_TEMPLATE = TEMPLATE_DIR / "quarterly_nav.tex.j2"
DEFAULT_CAPITAL_CALL_TEMPLATE = TEMPLATE_DIR / "capital_call.tex.j2"
DEFAULT_DISTRIBUTION_TEMPLATE = TEMPLATE_DIR / "distribution_notice.tex.j2"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LPReportingError(Exception):
    """Base error for the LP reporting pipeline."""


class TemplateNotFoundError(LPReportingError):
    """Raised when a configured Jinja2 template path does not exist."""


# ---------------------------------------------------------------------------
# Statement payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuarterlyStatement:
    """A single LP's sealed quarterly NAV statement.

    The ``tex_source`` is the byte-deterministic LaTeX rendering;
    ``content_digest`` is the SHA-256 of that source. Both fields
    are load-bearing for the LP portal contract: the portal serves
    ``tex_source`` (or a PDF compiled from it) and exposes the
    digest so an LP can verify the statement they downloaded matches
    the one the audit log references.
    """

    statement_id: str
    lp_id: str
    quarter_label: str
    quarter_start: date
    quarter_end: date
    generated_at: datetime
    nav: NAVSnapshot
    tex_source: str
    content_digest: str
    template_path: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assemble_quarterly_statement(
    *,
    commitment: LPCommitment,
    positions: Sequence[PortfolioPosition],
    marks: Mapping[str, Mark],
    cash_flows: Sequence[CashFlow],
    quarter_start: date,
    quarter_end: date,
    quarter_label: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    template_path: Optional[Path] = None,
    fund_name: str = "Coherence Fund",
    disclaimer: str = (
        "This statement is a record-only snapshot prepared by the Fund "
        "Administrator. It is not investment advice and does not "
        "constitute an offer to sell or a solicitation of an offer to "
        "buy any security."
    ),
) -> QuarterlyStatement:
    """Assemble one LP's sealed quarterly statement.

    Raises :class:`UnsignedMarkError` (from :mod:`nav_calculator`) if
    any held position lacks an operator-signed Mark. Raises
    :class:`TemplateNotFoundError` if ``template_path`` is missing.
    """

    snapshot = compute_nav(
        commitment=commitment,
        positions=positions,
        marks=marks,
        cash_flows=cash_flows,
        as_of=quarter_end,
    )

    label = quarter_label or _default_quarter_label(quarter_end)
    issued_at = generated_at or datetime.now(tz=timezone.utc)
    template = template_path or DEFAULT_QUARTERLY_TEMPLATE

    tex_source = render_quarterly_tex(
        snapshot=snapshot,
        quarter_label=label,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        generated_at=issued_at,
        cash_flows=cash_flows,
        fund_name=fund_name,
        disclaimer=disclaimer,
        template_path=template,
    )

    digest = compute_content_digest(tex_source)
    statement_id = _statement_id(commitment.lp_id, label, digest)

    return QuarterlyStatement(
        statement_id=statement_id,
        lp_id=commitment.lp_id,
        quarter_label=label,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        generated_at=issued_at,
        nav=snapshot,
        tex_source=tex_source,
        content_digest=digest,
        template_path=str(template),
    )


def assemble_batch(
    *,
    commitments: Iterable[LPCommitment],
    positions: Sequence[PortfolioPosition],
    marks: Mapping[str, Mark],
    cash_flows_by_lp: Mapping[str, Sequence[CashFlow]],
    quarter_start: date,
    quarter_end: date,
    **kwargs: Any,
) -> List[QuarterlyStatement]:
    """Assemble statements for every LP in ``commitments``.

    The ``cash_flows_by_lp`` lookup is keyed by ``lp_id``. An LP with
    no cash-flow history (e.g. a recent commit-only LP whose first
    capital call has not yet hit) gets an empty sequence rather than
    raising — :mod:`nav_calculator` handles the degenerate-IRR case.
    """

    out: List[QuarterlyStatement] = []
    for commitment in commitments:
        cash_flows = cash_flows_by_lp.get(commitment.lp_id, ())
        statement = assemble_quarterly_statement(
            commitment=commitment,
            positions=positions,
            marks=marks,
            cash_flows=cash_flows,
            quarter_start=quarter_start,
            quarter_end=quarter_end,
            **kwargs,
        )
        out.append(statement)
    return out


# ---------------------------------------------------------------------------
# Jinja2 / LaTeX
# ---------------------------------------------------------------------------


def latex_escape(value: Any) -> str:
    """Escape the small set of LaTeX special characters we may emit.

    The payloads we render here are governed (LP / company names,
    operator-attested mark methodology strings) but we still escape
    defensively — an LP entity name with an ``&`` would otherwise
    blow up the PDF compile.
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


def build_jinja_environment(template_dir: Path) -> jinja2.Environment:
    """Build the LP-reporting Jinja2 environment.

    Square-bracket delimiters keep ``{`` and ``}`` available for
    LaTeX literals; ``StrictUndefined`` makes typos in the template
    payload fail loudly rather than silently emitting an empty cell.
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
    env.filters["latex"] = latex_escape
    env.filters["usd"] = _format_usd
    env.filters["pct"] = _format_pct
    env.filters["irr"] = _format_irr
    env.filters["isoday"] = lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)
    return env


def render_quarterly_tex(
    *,
    snapshot: NAVSnapshot,
    quarter_label: str,
    quarter_start: date,
    quarter_end: date,
    generated_at: datetime,
    cash_flows: Sequence[CashFlow],
    fund_name: str,
    disclaimer: str,
    template_path: Path,
) -> str:
    """Render the quarterly-NAV ``.tex`` source from a NAV snapshot."""

    tpl_path = Path(template_path)
    if not tpl_path.is_file():
        raise TemplateNotFoundError(f"template not found: {tpl_path}")
    env = build_jinja_environment(tpl_path.parent)
    template = env.get_template(tpl_path.name)

    context = _quarterly_context(
        snapshot=snapshot,
        quarter_label=quarter_label,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        generated_at=generated_at,
        cash_flows=cash_flows,
        fund_name=fund_name,
        disclaimer=disclaimer,
    )
    rendered = template.render(**context)
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return rendered


def compute_content_digest(tex_source: str) -> str:
    """SHA-256 hex digest of the rendered ``.tex`` source."""

    return hashlib.sha256(tex_source.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _default_quarter_label(quarter_end: date) -> str:
    quarter = (quarter_end.month - 1) // 3 + 1
    return f"{quarter_end.year}Q{quarter}"


def _statement_id(lp_id: str, quarter_label: str, digest: str) -> str:
    payload = f"{lp_id}|{quarter_label}|{digest}".encode("utf-8")
    return "stmt_" + hashlib.sha256(payload).hexdigest()[:24]


def _format_usd(value: Any) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if f < 0 else ""
    return f"{sign}\\${abs(f):,.2f}"


def _format_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{f * 100.0:.2f}\\%"


def _format_irr(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f * 100.0:.2f}\\%"


def _quarterly_context(
    *,
    snapshot: NAVSnapshot,
    quarter_label: str,
    quarter_start: date,
    quarter_end: date,
    generated_at: datetime,
    cash_flows: Sequence[CashFlow],
    fund_name: str,
    disclaimer: str,
) -> Dict[str, Any]:
    return {
        "fund_name": fund_name,
        "disclaimer": disclaimer,
        "quarter_label": quarter_label,
        "quarter_start": quarter_start,
        "quarter_end": quarter_end,
        # A floor-second ISO timestamp keeps the rendered .tex stable
        # across runs that differ only in microseconds — the
        # determinism test pins ``generated_at`` so this is mostly
        # belt-and-braces.
        "generated_at": generated_at.replace(microsecond=0).isoformat(),
        "lp_id": snapshot.lp_id,
        "lp_legal_name": snapshot.legal_name,
        "as_of_date": snapshot.as_of_date,
        "commitment_usd": snapshot.commitment_usd,
        "called_to_date_usd": snapshot.called_to_date_usd,
        "uncalled_capital_usd": snapshot.uncalled_capital_usd,
        "distributions_to_date_usd": snapshot.distributions_to_date_usd,
        "ownership_fraction": snapshot.ownership_fraction,
        "total_cost_basis_usd": snapshot.total_cost_basis_usd,
        "total_fmv_usd": snapshot.total_fmv_usd,
        "unrealized_gain_usd": snapshot.unrealized_gain_usd,
        "nav_usd": snapshot.nav_usd,
        "irr": snapshot.irr,
        "has_positions": bool(snapshot.positions),
        "positions": [
            {
                "application_id": p.application_id,
                "company_name": p.company_name,
                "instrument_type": p.instrument_type,
                "fund_cost_basis_usd": p.fund_cost_basis_usd,
                "fund_fmv_usd": p.fund_fmv_usd,
                "lp_cost_basis_usd": p.lp_cost_basis_usd,
                "lp_fmv_usd": p.lp_fmv_usd,
                "lp_unrealized_gain_usd": p.lp_unrealized_gain_usd,
                "mark_methodology": p.mark_methodology,
                "mark_source": p.mark_source,
                "mark_as_of": p.mark_as_of,
            }
            for p in snapshot.positions
        ],
        "has_cash_flows": bool(cash_flows),
        "cash_flows": [
            {
                "flow_date": c.flow_date,
                "amount_usd": c.amount_usd,
                "kind": c.kind,
            }
            for c in sorted(cash_flows, key=lambda x: (x.flow_date, x.kind))
        ],
    }
