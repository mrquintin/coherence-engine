"""Capital-call notice rendering (prompt 69).

A capital-call notice tells one LP that a portion of their committed
capital is being drawn against a specific portfolio investment. The
notice is rendered as a deterministic ``.tex`` source from a Jinja2
template; PDF compilation re-uses
:mod:`server.fund.services.model_risk_renderer_pdf`'s ``pdflatex``
runner pattern.

Lifecycle and prohibitions
--------------------------

A notice is **not** a money-mover. The capital-deployment service
(prompt 51) is the single sanctioned execution path for funds; this
module is purely a document renderer plus a thin DocuSign hand-off
helper. The dispatch helper :func:`dispatch_for_signature` requires
a caller-supplied :class:`ESignatureProvider` (typically the
DocuSign backend wired up in prompt 52) and is responsible for:

* assembling the LP signer plus a fund-side counter-signer;
* rendering the ``PreparedDocument`` from the notice payload; and
* delegating ``send`` to the provider with a deterministic
  idempotency key derived from ``(lp_id, call_id)``.

The dispatch helper does NOT update the database — that is the
caller's responsibility, mirroring the prompt-52 pattern where the
service layer owns persistence and the renderer owns the in-memory
document body.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


from coherence_engine.server.fund.services.lp_reporting import (
    DEFAULT_CAPITAL_CALL_TEMPLATE,
    TemplateNotFoundError,
    build_jinja_environment,
    compute_content_digest,
)


__all__ = [
    "CapitalCallLineItem",
    "CapitalCallNotice",
    "RenderedCapitalCall",
    "CapitalCallError",
    "render_notice",
    "render_pdf",
    "compute_idempotency_key",
    "dispatch_for_signature",
]


_LOG = logging.getLogger(__name__)


# Standard 10-business-day funding window for pre-seed funds; surfaced
# as a default rather than hard-coded so the operator can override per
# call (e.g. shortened windows for bridge rounds).
DEFAULT_DUE_WINDOW_DAYS = 10


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CapitalCallError(Exception):
    """Raised by the capital-call renderer / dispatch helper."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapitalCallLineItem:
    """One investment-level line in a capital-call notice."""

    application_id: str
    company_name: str
    instrument_type: str
    investment_amount_usd: float
    lp_share_usd: float


@dataclass(frozen=True)
class CapitalCallNotice:
    """A single LP's capital-call notice payload.

    The ``call_id`` is the fund-wide identifier of the call (e.g.
    ``CC-2026Q2-01``); :func:`compute_idempotency_key` derives a
    per-LP idempotency key from it for the e-signature dispatch.
    """

    call_id: str
    lp_id: str
    lp_legal_name: str
    fund_name: str
    notice_date: date
    due_date: date
    total_call_amount_usd: float
    lp_call_amount_usd: float
    cumulative_called_usd: float
    remaining_commitment_usd: float
    line_items: Sequence[CapitalCallLineItem]
    wire_instructions_ref: str
    contact_email: str
    disclaimer: str = (
        "This notice constitutes a binding capital call under the "
        "Limited Partnership Agreement. Funds are due in cleared "
        "USD by the due date stated above. Late payments may accrue "
        "interest at the rate specified in the LPA."
    )


@dataclass(frozen=True)
class RenderedCapitalCall:
    """Output of :func:`render_notice` -- ``.tex`` source + digest."""

    tex_source: str
    content_digest: str
    template_path: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_notice(
    notice: CapitalCallNotice,
    *,
    template_path: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> RenderedCapitalCall:
    """Render a capital-call ``.tex`` source from the notice payload."""

    tpl_path = Path(template_path) if template_path else DEFAULT_CAPITAL_CALL_TEMPLATE
    if not tpl_path.is_file():
        raise TemplateNotFoundError(f"template not found: {tpl_path}")

    env = build_jinja_environment(tpl_path.parent)
    template = env.get_template(tpl_path.name)

    issued_at = (generated_at or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    context = _build_context(notice, generated_at=issued_at)
    rendered = template.render(**context)
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return RenderedCapitalCall(
        tex_source=rendered,
        content_digest=compute_content_digest(rendered),
        template_path=str(tpl_path),
    )


def render_pdf(
    notice: CapitalCallNotice,
    *,
    template_path: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> bytes:
    """Render the notice and compile it through ``pdflatex``.

    Re-uses the existing MRM-renderer's ``pdflatex`` invocation —
    same isolated temp directory, same two-pass settle. We only
    expose the bytes here; the caller is responsible for routing them
    to object storage.
    """
    # Lazy import keeps ``render_notice`` (the byte-deterministic
    # path the tests exercise) decoupled from the pdflatex check.
    from coherence_engine.server.fund.services.model_risk_renderer_pdf import (
        PDFLATEX_EXECUTABLE,
        PDFLATEX_PASS_COUNT,
        PDFLATEX_TIMEOUT_SECONDS,
        PdflatexNotInstalled,
        PdflatexRenderError,
    )
    import shutil
    import subprocess
    import tempfile

    if shutil.which(PDFLATEX_EXECUTABLE) is None:
        raise PdflatexNotInstalled(
            "pdflatex executable not found on PATH — install MacTeX / TeX Live "
            "or render the .tex source via render_notice() and compile out-of-band."
        )

    tex_source = render_notice(
        notice, template_path=template_path, generated_at=generated_at
    ).tex_source

    with tempfile.TemporaryDirectory(prefix="capital-call-") as tmp:
        work = Path(tmp)
        tex_path = work / "notice.tex"
        tex_path.write_bytes(tex_source.encode("utf-8"))
        last_log = ""
        for _ in range(PDFLATEX_PASS_COUNT):
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
            )
            log_path = work / "notice.log"
            if log_path.is_file():
                last_log = log_path.read_text(encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                raise PdflatexRenderError(
                    "pdflatex failed for capital-call notice",
                    log_text=last_log,
                    returncode=proc.returncode,
                )
        pdf_path = work / "notice.pdf"
        if not pdf_path.is_file():
            raise PdflatexRenderError(
                "pdflatex produced no PDF output", log_text=last_log
            )
        return pdf_path.read_bytes()


def compute_idempotency_key(call_id: str, lp_id: str) -> str:
    """Deterministic idempotency key for a per-LP capital-call dispatch.

    A repeat call to :func:`dispatch_for_signature` for the same
    ``(call_id, lp_id)`` pair MUST collapse onto a single e-signature
    envelope; this is the key the DocuSign backend keys off.
    """
    payload = f"capital_call|{call_id}|{lp_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dispatch_for_signature(
    notice: CapitalCallNotice,
    *,
    provider: Any,  # ESignatureProvider; typed Any to avoid hard import cycle
    fund_signer_name: str,
    fund_signer_email: str,
    template_path: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> Mapping[str, str]:
    """Render the notice and hand it to an :class:`ESignatureProvider`.

    Returns a small dict containing the provider's request id, the
    deterministic idempotency key, and the content digest of the
    rendered .tex source. The caller is responsible for persisting
    this triple alongside the local notice row.
    """
    # Lazy import to avoid creating a hard dependency cycle between
    # the LP-reporting and e-signature subsystems at import time.
    from coherence_engine.server.fund.services.esignature import (
        PreparedDocument,
        Signer,
    )

    if not notice.lp_id or not notice.call_id:
        raise CapitalCallError("call_id and lp_id are required")

    rendered = render_notice(
        notice, template_path=template_path, generated_at=generated_at
    )
    idem_key = compute_idempotency_key(notice.call_id, notice.lp_id)

    document = PreparedDocument(
        template_id="capital_call_notice_v1",
        body=rendered.tex_source.encode("utf-8"),
        vars_hash=rendered.content_digest,
        content_type="application/x-latex",
    )
    signers = (
        Signer(
            name=notice.lp_legal_name,
            email=notice.contact_email,
            role="lp_signer",
        ),
        Signer(
            name=fund_signer_name,
            email=fund_signer_email,
            role="fund_countersigner",
        ),
    )

    response = provider.send(
        document=document,
        signers=signers,
        idempotency_key=idem_key,
    )
    return {
        "provider_request_id": response.provider_request_id,
        "idempotency_key": idem_key,
        "content_digest": rendered.content_digest,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_context(
    notice: CapitalCallNotice, *, generated_at: datetime
) -> Dict[str, Any]:
    return {
        "fund_name": notice.fund_name,
        "call_id": notice.call_id,
        "lp_id": notice.lp_id,
        "lp_legal_name": notice.lp_legal_name,
        "notice_date": notice.notice_date,
        "due_date": notice.due_date,
        "generated_at": generated_at.isoformat(),
        "total_call_amount_usd": notice.total_call_amount_usd,
        "lp_call_amount_usd": notice.lp_call_amount_usd,
        "cumulative_called_usd": notice.cumulative_called_usd,
        "remaining_commitment_usd": notice.remaining_commitment_usd,
        "wire_instructions_ref": notice.wire_instructions_ref,
        "contact_email": notice.contact_email,
        "disclaimer": notice.disclaimer,
        "has_line_items": bool(notice.line_items),
        "line_items": [
            {
                "application_id": li.application_id,
                "company_name": li.company_name,
                "instrument_type": li.instrument_type,
                "investment_amount_usd": li.investment_amount_usd,
                "lp_share_usd": li.lp_share_usd,
            }
            for li in notice.line_items
        ],
    }
