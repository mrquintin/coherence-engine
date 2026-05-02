"""Distribution-notice rendering (prompt 69).

A distribution notice tells one LP that the fund intends to return
capital and/or proceeds against a realized portfolio event. As with
the capital-call notice this module is a *renderer* — the actual
movement of money is the treasurer's responsibility under the
prompt-51 capital-deployment lifecycle.

Prompt-69 prohibition (load-bearing)
------------------------------------

This module MUST NOT execute or schedule any transfer. The only
side effect of :func:`dispatch_for_acknowledgement` is to send the
notice through an :class:`ESignatureProvider` for the LP to
acknowledge receipt. Bank instructions are a *reference token*
(an opaque ``wire_instructions_ref``) that points at the treasurer-
controlled wire register; the raw account / routing numbers never
enter this payload, mirroring the prompt-51 storage discipline on
:class:`InvestmentInstruction.target_account_ref`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from coherence_engine.server.fund.services.lp_reporting import (
    DEFAULT_DISTRIBUTION_TEMPLATE,
    TemplateNotFoundError,
    build_jinja_environment,
    compute_content_digest,
)


__all__ = [
    "DistributionLineItem",
    "DistributionNotice",
    "RenderedDistribution",
    "DistributionNoticeError",
    "render_notice",
    "compute_idempotency_key",
    "dispatch_for_acknowledgement",
]


_LOG = logging.getLogger(__name__)


ALLOWED_DISTRIBUTION_KINDS = frozenset(
    {"return_of_capital", "realized_gain", "interest", "other"}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DistributionNoticeError(Exception):
    """Raised by the distribution-notice renderer / dispatch helper."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionLineItem:
    """One line of the distribution waterfall."""

    application_id: str
    company_name: str
    kind: str  # one of ALLOWED_DISTRIBUTION_KINDS
    gross_amount_usd: float
    lp_share_usd: float


@dataclass(frozen=True)
class DistributionNotice:
    """Per-LP distribution notice payload.

    The ``treasurer_approval_ref`` is the
    :class:`TreasurerApproval` row id (prompt 51) that authorises
    the underlying wire — without it the notice would be advisory
    only, which is the prohibition this prompt is enforcing.
    """

    distribution_id: str
    lp_id: str
    lp_legal_name: str
    fund_name: str
    notice_date: date
    payment_date: date
    total_distribution_usd: float
    lp_distribution_usd: float
    cumulative_distributions_usd: float
    line_items: Sequence[DistributionLineItem]
    wire_instructions_ref: str
    treasurer_approval_ref: str
    contact_email: str
    disclaimer: str = (
        "This notice is a record of the Fund's intent to distribute "
        "the proceeds described above on or about the payment date. "
        "Actual settlement is contingent on the treasurer's execution "
        "of the underlying wire transfer; this software does not move "
        "funds. Tax characterisation will be communicated separately "
        "via the annual K-1."
    )


@dataclass(frozen=True)
class RenderedDistribution:
    """Output of :func:`render_notice` -- ``.tex`` source + digest."""

    tex_source: str
    content_digest: str
    template_path: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_notice(
    notice: DistributionNotice,
    *,
    template_path: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> RenderedDistribution:
    """Render a distribution-notice ``.tex`` source from the payload."""

    _validate_notice(notice)

    tpl_path = Path(template_path) if template_path else DEFAULT_DISTRIBUTION_TEMPLATE
    if not tpl_path.is_file():
        raise TemplateNotFoundError(f"template not found: {tpl_path}")
    env = build_jinja_environment(tpl_path.parent)
    template = env.get_template(tpl_path.name)

    issued_at = (generated_at or datetime.now(tz=timezone.utc)).replace(microsecond=0)
    context = _build_context(notice, generated_at=issued_at)
    rendered = template.render(**context)
    if not rendered.endswith("\n"):
        rendered = rendered + "\n"
    return RenderedDistribution(
        tex_source=rendered,
        content_digest=compute_content_digest(rendered),
        template_path=str(tpl_path),
    )


def compute_idempotency_key(distribution_id: str, lp_id: str) -> str:
    """Deterministic idempotency key for a per-LP distribution acknowledgement."""

    payload = f"distribution|{distribution_id}|{lp_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dispatch_for_acknowledgement(
    notice: DistributionNotice,
    *,
    provider: Any,  # ESignatureProvider; typed Any to dodge import cycle
    fund_signer_name: str,
    fund_signer_email: str,
    template_path: Optional[Path] = None,
    generated_at: Optional[datetime] = None,
) -> Mapping[str, str]:
    """Dispatch the notice for LP acknowledgement.

    The acknowledgement is a *receipt* signature — it does not
    constitute consent to a wire transfer (the LPA already grants
    that authority on a signed capital call). The treasurer remains
    responsible for executing the wire under prompt 51.
    """

    from coherence_engine.server.fund.services.esignature import (
        PreparedDocument,
        Signer,
    )

    if not notice.distribution_id or not notice.lp_id:
        raise DistributionNoticeError(
            "distribution_id and lp_id are required"
        )
    if not notice.treasurer_approval_ref:
        raise DistributionNoticeError(
            "treasurer_approval_ref is required — distribution notices "
            "may not be dispatched without a matching prompt-51 approval row"
        )

    rendered = render_notice(
        notice, template_path=template_path, generated_at=generated_at
    )
    idem_key = compute_idempotency_key(notice.distribution_id, notice.lp_id)

    document = PreparedDocument(
        template_id="distribution_notice_v1",
        body=rendered.tex_source.encode("utf-8"),
        vars_hash=rendered.content_digest,
        content_type="application/x-latex",
    )
    signers = (
        Signer(
            name=notice.lp_legal_name,
            email=notice.contact_email,
            role="lp_acknowledger",
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


def _validate_notice(notice: DistributionNotice) -> None:
    if notice.lp_distribution_usd < 0:
        raise DistributionNoticeError(
            "lp_distribution_usd must be non-negative"
        )
    if notice.total_distribution_usd < notice.lp_distribution_usd:
        raise DistributionNoticeError(
            "total_distribution_usd must be >= lp_distribution_usd"
        )
    if not notice.wire_instructions_ref:
        raise DistributionNoticeError(
            "wire_instructions_ref is required (raw bank details "
            "must NOT be embedded in the notice payload)"
        )
    for line in notice.line_items:
        if line.kind not in ALLOWED_DISTRIBUTION_KINDS:
            raise DistributionNoticeError(
                f"distribution line kind {line.kind!r} is not recognised"
            )
        if line.gross_amount_usd < 0 or line.lp_share_usd < 0:
            raise DistributionNoticeError(
                "distribution line amounts must be non-negative"
            )


def _build_context(
    notice: DistributionNotice, *, generated_at: datetime
) -> Dict[str, Any]:
    return {
        "fund_name": notice.fund_name,
        "distribution_id": notice.distribution_id,
        "lp_id": notice.lp_id,
        "lp_legal_name": notice.lp_legal_name,
        "notice_date": notice.notice_date,
        "payment_date": notice.payment_date,
        "generated_at": generated_at.isoformat(),
        "total_distribution_usd": notice.total_distribution_usd,
        "lp_distribution_usd": notice.lp_distribution_usd,
        "cumulative_distributions_usd": notice.cumulative_distributions_usd,
        "wire_instructions_ref": notice.wire_instructions_ref,
        "treasurer_approval_ref": notice.treasurer_approval_ref,
        "contact_email": notice.contact_email,
        "disclaimer": notice.disclaimer,
        "has_line_items": bool(notice.line_items),
        "line_items": [
            {
                "application_id": li.application_id,
                "company_name": li.company_name,
                "kind": li.kind,
                "gross_amount_usd": li.gross_amount_usd,
                "lp_share_usd": li.lp_share_usd,
            }
            for li in notice.line_items
        ],
    }
