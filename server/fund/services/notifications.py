"""Notification dispatch service (prompt 14).

This module implements the founder-facing + partner-facing notification
service consumed by ``ApplicationService.process_next_scoring_job`` after
an enforce-mode decision has been written. In shadow mode the dispatch
is suppressed by the caller (per prompt 12).

Pipeline
--------

::

    decision_issued (enforce mode)
        -> dispatch(session, application_id, verdict, *, backend, ...)
            -> select template ('founder_pass' | 'founder_reject' |
               'founder_manual_review' | 'partner_escalation')
            -> render with str.format_map(context)
            -> idempotency check on
               sha256(application_id|template_id)
            -> backend.send(to, subject, body)
            -> persist NotificationLog row (status='sent' | 'failed')

Idempotency
-----------

``idempotency_key = sha256(f"{application_id}|{template_id}").hexdigest()``

A successful (``status='sent'``) row blocks re-send: the second call to
``dispatch`` with the same ``(application_id, template_id)`` returns the
existing log row unchanged. A failed row is *not* a permanent block —
re-dispatch is allowed and updates the same row in place (so retries
preserve the unique key invariant).

Placeholder contract (templates render via ``str.format_map``)
--------------------------------------------------------------

Every founder template (``founder_pass.txt``, ``founder_reject.txt``,
``founder_manual_review.txt``) and the partner template
(``partner_escalation.md``) accepts the following placeholders. Missing
keys are filled with ``"-"`` to make rendering total (no
``KeyError`` at runtime when an upstream record is partially populated).

* ``founder_name``         — full name from ``Founder.full_name``
* ``founder_email``        — email from ``Founder.email``
* ``company_name``         — company name from ``Founder.company_name``
* ``application_id``       — the application id
* ``decision``             — canonical verdict
  (``pass | reject | manual_review``)
* ``policy_version``       — decision-policy version string
* ``coherence_observed``   — float, decision policy output
* ``threshold_required``   — float, decision policy output
* ``margin``               — float, decision policy output
* ``failed_gates_summary`` — comma-joined ``reason_code`` list, or
  ``"none"`` when there are no failed gates

Backends
--------

The actual transport step is delegated to a
:class:`~coherence_engine.server.fund.services.notification_backends.NotificationBackend`
implementation. ``DryRunBackend`` is the default in CI and writes
JSON envelopes under ``dry_run_dir`` instead of opening sockets.

Prohibitions (prompt 14):

* No real emails are sent from any test (the SMTP / SES / Sendgrid
  backends are env-gated and never invoked under default CI).
* Raw credentials are NEVER persisted into the ``NotificationLog``
  row (only the rendered ``recipient`` address, the channel name,
  and operator-readable ``status``/``error`` strings).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.notification_backends import (
    NotificationBackend,
    NotificationBackendError,
)


__all__ = [
    "TEMPLATE_FOUNDER_PASS",
    "TEMPLATE_FOUNDER_REJECT",
    "TEMPLATE_FOUNDER_MANUAL_REVIEW",
    "TEMPLATE_PARTNER_ESCALATION",
    "VERDICT_TO_FOUNDER_TEMPLATE",
    "NotificationError",
    "compute_idempotency_key",
    "dispatch",
    "render_template",
    "load_template",
    "build_context",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------


TEMPLATE_FOUNDER_PASS = "founder_pass"
TEMPLATE_FOUNDER_REJECT = "founder_reject"
TEMPLATE_FOUNDER_MANUAL_REVIEW = "founder_manual_review"
TEMPLATE_PARTNER_ESCALATION = "partner_escalation"


# Verdict-to-template mapping is total: every canonical verdict from
# ``decision_issued.v1.json`` ('pass' | 'reject' | 'manual_review')
# maps to exactly one founder template. The partner-escalation template
# is dispatched by a separate caller; it is not part of this map.
VERDICT_TO_FOUNDER_TEMPLATE: Dict[str, str] = {
    "pass": TEMPLATE_FOUNDER_PASS,
    "reject": TEMPLATE_FOUNDER_REJECT,
    "manual_review": TEMPLATE_FOUNDER_MANUAL_REVIEW,
}


_TEMPLATE_FILES: Dict[str, str] = {
    TEMPLATE_FOUNDER_PASS: "founder_pass.txt",
    TEMPLATE_FOUNDER_REJECT: "founder_reject.txt",
    TEMPLATE_FOUNDER_MANUAL_REVIEW: "founder_manual_review.txt",
    TEMPLATE_PARTNER_ESCALATION: "partner_escalation.md",
}


_TEMPLATES_ROOT = (
    Path(__file__).resolve().parent.parent / "data" / "notification_templates"
)


# Subject lines pulled from the first ``Subject:`` header line in the
# rendered body, when present. The Markdown partner template does not
# carry a Subject header — we synthesize one for it from the rendered
# context.
_SUBJECT_PREFIX_RE = re.compile(r"^Subject:\s*(.*?)\r?\n", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotificationError(Exception):
    """Raised by ``dispatch`` for any non-recoverable failure.

    Wraps backend transport errors, missing template errors, and
    validation errors (unknown verdict, missing application). The
    string form is operator-readable and is what gets persisted into
    the ``NotificationLog.error`` column.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def compute_idempotency_key(application_id: str, template_id: str) -> str:
    """Return ``sha256(f"{application_id}|{template_id}").hexdigest()``.

    The key is what gets persisted into ``NotificationLog.idempotency_key``
    and is uniqueness-enforced by the table's unique index. Both inputs
    are coerced to ``str`` before hashing so a non-string id never
    silently produces a different digest.
    """
    payload = f"{application_id!s}|{template_id!s}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_template(template_id: str) -> str:
    """Load the raw template body for ``template_id`` from the on-disk registry.

    Raises :class:`NotificationError` if the template id is unknown
    or the file cannot be read.
    """
    if template_id not in _TEMPLATE_FILES:
        raise NotificationError(f"unknown_template:{template_id!r}")
    path = _TEMPLATES_ROOT / _TEMPLATE_FILES[template_id]
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotificationError(
            f"template_file_not_found:{template_id}:{path}"
        ) from exc


class _DefaultDashDict(dict):
    """str.format_map adapter that returns ``"-"`` for missing keys.

    Keeps templates total even when an upstream record is partially
    populated — the alternative is a noisy ``KeyError`` that surfaces
    only at runtime under a specific data shape.
    """

    def __missing__(self, key: str) -> str:
        return "-"


def render_template(template_body: str, context: Mapping[str, object]) -> str:
    """Render ``template_body`` against ``context`` via ``str.format_map``.

    Missing keys are filled with ``"-"`` (see :class:`_DefaultDashDict`).
    Numeric values are not re-formatted; callers typically pre-round
    floats to 6 decimals in :func:`build_context`.
    """
    return template_body.format_map(_DefaultDashDict(context))


def _extract_subject_and_body(rendered: str, fallback_subject: str) -> tuple[str, str]:
    """Split ``Subject: ...`` header (if present) from the rendered body."""
    match = _SUBJECT_PREFIX_RE.match(rendered)
    if match:
        subject = match.group(1).strip()
        body = rendered[match.end():].lstrip("\r\n")
        return subject, body
    return fallback_subject, rendered


def build_context(
    *,
    application: Any,
    founder: Any,
    decision: Any,
    extra: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    """Assemble the placeholder context for a single dispatch.

    Pulls fields off the ORM-resolved ``application`` + ``founder`` +
    ``decision`` rows; ``extra`` overrides individual keys when the
    caller already has a richer payload (e.g. when called from the
    in-memory worker test that doesn't load fresh ORM rows).

    Numeric outputs of the decision policy are rounded to 6 decimals
    so two runs against the same persisted state produce byte-
    identical templates.
    """
    failed_gates = []
    failed_gates_raw = getattr(decision, "failed_gates_json", None)
    if failed_gates_raw:
        try:
            import json

            parsed = json.loads(failed_gates_raw)
            if isinstance(parsed, list):
                failed_gates = [
                    str(g.get("reason_code", ""))
                    for g in parsed
                    if isinstance(g, dict) and g.get("reason_code")
                ]
        except (TypeError, ValueError):
            failed_gates = []

    canonical = str(getattr(decision, "decision", "")).strip()
    if canonical == "fail":
        canonical = "reject"

    ctx: Dict[str, object] = {
        "founder_name": str(getattr(founder, "full_name", "") or "-"),
        "founder_email": str(getattr(founder, "email", "") or "-"),
        "company_name": str(getattr(founder, "company_name", "") or "-"),
        "application_id": str(getattr(application, "id", "") or "-"),
        "decision": canonical or "-",
        "policy_version": str(
            getattr(decision, "decision_policy_version", None)
            or getattr(decision, "policy_version", "")
            or "-"
        ),
        "coherence_observed": round(
            float(getattr(decision, "coherence_observed", 0.0) or 0.0), 6
        ),
        "threshold_required": round(
            float(getattr(decision, "threshold_required", 0.0) or 0.0), 6
        ),
        "margin": round(float(getattr(decision, "margin", 0.0) or 0.0), 6),
        "failed_gates_summary": ", ".join(failed_gates) if failed_gates else "none",
    }
    if extra:
        for k, v in extra.items():
            ctx[str(k)] = v
    return ctx


# ---------------------------------------------------------------------------
# Verdict -> template selection
# ---------------------------------------------------------------------------


def template_id_for_verdict(verdict: str) -> str:
    """Map a canonical decision verdict to a founder template id.

    Treats the legacy decision-policy ``fail`` value as ``reject`` so
    callers that pass through the policy output verbatim still resolve
    correctly.
    """
    canonical = str(verdict or "").strip().lower()
    if canonical == "fail":
        canonical = "reject"
    if canonical not in VERDICT_TO_FOUNDER_TEMPLATE:
        raise NotificationError(
            f"unknown_verdict:{verdict!r}; "
            f"expected one of {sorted(VERDICT_TO_FOUNDER_TEMPLATE)}"
        )
    return VERDICT_TO_FOUNDER_TEMPLATE[canonical]


# ---------------------------------------------------------------------------
# Public dispatch entry point
# ---------------------------------------------------------------------------


def dispatch(
    session: Session,
    application_id: str,
    verdict: str,
    *,
    backend: NotificationBackend,
    template_id: Optional[str] = None,
    recipient: Optional[str] = None,
    extra_context: Optional[Mapping[str, object]] = None,
) -> models.NotificationLog:
    """Dispatch a notification for ``application_id`` and persist a log row.

    Args:
        session: An open SQLAlchemy session. The caller is responsible
            for committing; this function flushes but does not commit.
        application_id: The id of the application being notified about.
        verdict: Canonical verdict (``pass | reject | manual_review``)
            used to pick the founder template when ``template_id`` is
            omitted. The legacy ``fail`` value is treated as
            ``reject``.
        backend: A :class:`NotificationBackend` implementation. The
            ``DryRunBackend`` is the default in CI; production
            callers can pass an SMTP / SES / Sendgrid backend.
        template_id: Override the verdict-derived template id (used
            for the partner-escalation template).
        recipient: Override the resolved to-address (defaults to the
            founder's email for founder templates).
        extra_context: Additional placeholder values merged on top of
            the auto-derived context (used by tests + by the partner-
            escalation caller).

    Returns:
        The persisted :class:`~coherence_engine.server.fund.models.NotificationLog`
        row. If a row with the same ``idempotency_key`` already exists
        in ``status="sent"``, the existing row is returned unchanged
        (no transport call, no body re-render).

    Raises:
        NotificationError: on unknown verdict / template, missing
            application, or backend transport failure (after the
            failure has been recorded as ``status="failed"``).
    """
    selected_template = template_id or template_id_for_verdict(verdict)

    idempotency_key = compute_idempotency_key(application_id, selected_template)

    existing = (
        session.query(models.NotificationLog)
        .filter(models.NotificationLog.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if existing is not None and existing.status == "sent":
        return existing

    application = (
        session.query(models.Application)
        .filter(models.Application.id == application_id)
        .one_or_none()
    )
    if application is None:
        raise NotificationError(f"application_not_found:{application_id}")

    founder = (
        session.query(models.Founder)
        .filter(models.Founder.id == application.founder_id)
        .one_or_none()
    )
    decision = (
        session.query(models.Decision)
        .filter(models.Decision.application_id == application_id)
        .one_or_none()
    )
    if decision is None:
        raise NotificationError(f"decision_not_available:{application_id}")

    context = build_context(
        application=application,
        founder=founder,
        decision=decision,
        extra=extra_context,
    )

    template_body = load_template(selected_template)
    rendered = render_template(template_body, context)

    fallback_subject = (
        f"[fund] {selected_template} — application {application_id}"
    )
    subject, body = _extract_subject_and_body(rendered, fallback_subject)

    resolved_recipient = recipient or str(context.get("founder_email") or "")
    if not resolved_recipient or resolved_recipient == "-":
        raise NotificationError(
            f"recipient_unresolved:{application_id}:{selected_template}"
        )

    log = existing or models.NotificationLog(
        id=f"ntf_{uuid.uuid4().hex[:16]}",
        application_id=application_id,
        template_id=selected_template,
        channel=str(getattr(backend, "channel", "") or "unknown"),
        recipient=resolved_recipient,
        idempotency_key=idempotency_key,
        status="pending",
        error="",
        created_at=_utc_now(),
        sent_at=None,
    )
    if existing is None:
        session.add(log)
        session.flush()
    else:
        log.channel = str(getattr(backend, "channel", "") or "unknown")
        log.recipient = resolved_recipient
        log.status = "pending"
        log.error = ""
        log.sent_at = None

    try:
        backend.send(to=resolved_recipient, subject=subject, body=body)
    except NotificationBackendError as exc:
        log.status = "failed"
        log.error = str(exc)[:1000]
        log.sent_at = None
        session.flush()
        raise NotificationError(
            f"notification_send_failed:{selected_template}:{exc}"
        ) from exc
    except Exception as exc:
        log.status = "failed"
        log.error = f"unexpected:{type(exc).__name__}"
        log.sent_at = None
        session.flush()
        raise NotificationError(
            f"notification_send_unexpected:{selected_template}:{type(exc).__name__}"
        ) from exc

    log.status = "sent"
    log.error = ""
    log.sent_at = _utc_now()
    session.flush()
    return log
