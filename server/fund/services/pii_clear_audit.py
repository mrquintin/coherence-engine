"""Scope-gated clear-PII reads with an immutable audit log (prompt 58).

Background
----------

PII columns are stored on disk as deterministic tokens (see
:mod:`pii_tokenization`) and an encrypted clear copy under per-row
AES-GCM (see :mod:`per_row_encryption`). The default ORM accessor for a
PII attribute returns the *token*; the clear value is reachable only
through the helpers in this module, which:

1. Verify that the calling principal carries the
   ``pii:read_clear`` scope. Calls without the scope raise
   :class:`PermissionError`, which the routers map to HTTP 403.
2. Decrypt the ciphertext column under the per-row key id.
3. Insert a :class:`PIIClearAuditLog` row recording who read what,
   for which subject, from which route, with the matching request id.
   The row is committed in the same transaction as the read so a
   crash mid-read either leaves no audit trail *and* no clear value
   surfaced (atomic), or both succeed.

The audit log table is INSERT-only at the database level (see RLS in
``server/fund/security/rls.py``); UPDATE / DELETE attempts are denied
even for the service role. This protects against an operator with write
access tampering with the trail of clear-PII accesses.

This module is the only sanctioned path to clear PII. Direct reads of
``email_clear`` / ``email_clear_key_id`` outside the helpers below are a
review-blocking violation — see ``docs/specs/pii_handling.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence
import uuid

from sqlalchemy import (
    DateTime,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from coherence_engine.server.fund.database import Base
from coherence_engine.server.fund.services.per_row_encryption import decrypt


PII_READ_CLEAR_SCOPE = "pii:read_clear"


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class PIIClearAuditLog(Base):
    """Immutable audit row written every time a clear PII value is surfaced.

    Schema discipline: this table never holds the clear value itself —
    only the *token* form, the field name, the subject id, the
    principal id, and the route / request id. Reading the audit log
    therefore tells an investigator who saw which PII record but not
    what value they saw — which is exactly what a non-bypassable audit
    log of PII reads should look like.

    INSERT-only at the DB layer: the matching RLS policies in
    :mod:`server.fund.security.rls` deny UPDATE / DELETE for every
    declared role, including ``service_role``.
    """

    __tablename__ = "pii_clear_audit_log"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Application-layer principal id: API key prefix, service account
    # id, or Supabase user id — whatever the caller's auth dep yielded.
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    principal_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="api_key"
    )
    # The PII *kind* (email / phone / name / address) — never the
    # clear value.
    field_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Tokenized form of the value being read. Safe to log.
    token: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # The id of the row whose PII is being surfaced (e.g. founder_id).
    subject_table: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Where the read came from. ``request_id`` correlates with the
    # API gateway access log; ``route`` is the FastAPI path template.
    route: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    reason: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Free-form note (kept short; never the clear value).
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, index=True
    )


@dataclass(frozen=True)
class ClearReadPrincipal:
    """Minimal principal shape accepted by :func:`read_clear`.

    The router-level dep adapts the ``ApiKey`` model (or Supabase
    ``current_user``) into this shape so this module has no FastAPI /
    framework coupling — handy for unit tests.
    """

    id: str
    kind: str = "api_key"
    scopes: Sequence[str] = ()

    def has_scope(self, scope: str) -> bool:
        return scope in set(self.scopes)


class ClearReadDenied(PermissionError):
    """Raised when a clear-PII read is attempted without the required scope."""


def _new_audit_id() -> str:
    return f"piiaud_{uuid.uuid4().hex[:24]}"


def read_clear(
    *,
    db: Session,
    principal: ClearReadPrincipal,
    field_kind: str,
    token: str,
    ciphertext_b64: str,
    key_id: str,
    subject_table: str,
    subject_id: str,
    route: str = "",
    request_id: str = "",
    reason: str = "",
) -> str:
    """Decrypt a clear-PII column and record the access.

    Parameters
    ----------
    db:
        Active SQLAlchemy session. The audit row is added (and flushed)
        on this session; the caller controls the surrounding commit so
        the read + audit are atomic with the request's transaction.
    principal:
        Adapter around the calling identity. Must carry
        :data:`PII_READ_CLEAR_SCOPE`.
    field_kind:
        One of the kinds in :mod:`pii_tokenization` (e.g. ``email``).
    token:
        Tokenized form of the value being read. Recorded in the audit
        row; **never log the clear return value**.
    ciphertext_b64 / key_id:
        Per-row encryption blob and key reference. Pass through from
        the model row.
    subject_table / subject_id:
        Identify the row whose PII is being surfaced.

    Returns
    -------
    The clear PII value as a Python ``str``.

    Raises
    ------
    ClearReadDenied
        If ``principal`` lacks the ``pii:read_clear`` scope.
    """
    if not principal.has_scope(PII_READ_CLEAR_SCOPE):
        raise ClearReadDenied(
            f"principal {principal.id!r} lacks required scope "
            f"{PII_READ_CLEAR_SCOPE!r} for clear-PII read"
        )
    if not ciphertext_b64 or not key_id:
        raise ValueError("clear column has no ciphertext or key_id; nothing to read")

    plaintext_bytes = decrypt(
        ciphertext_b64,
        db=db,
        row_id=subject_id,
        key_id=key_id,
    )
    clear = plaintext_bytes.decode("utf-8")

    audit = PIIClearAuditLog(
        id=_new_audit_id(),
        principal_id=principal.id,
        principal_kind=principal.kind,
        field_kind=field_kind,
        token=token,
        subject_table=subject_table,
        subject_id=subject_id,
        route=route,
        request_id=request_id,
        reason=reason,
    )
    db.add(audit)
    db.flush()
    return clear


def adapt_api_key(
    api_key, *, scopes: Optional[Sequence[str]] = None
) -> ClearReadPrincipal:
    """Adapt a ``models.ApiKey`` row into a :class:`ClearReadPrincipal`.

    ``scopes`` may be passed explicitly when the caller has already
    decoded ``api_key.scopes_json``; otherwise this function decodes
    it itself.
    """
    if scopes is None:
        import json

        try:
            scopes = json.loads(getattr(api_key, "scopes_json", "") or "[]")
        except (TypeError, ValueError):
            scopes = []
    return ClearReadPrincipal(
        id=str(getattr(api_key, "prefix", getattr(api_key, "id", ""))),
        kind="api_key",
        scopes=tuple(str(s) for s in scopes),
    )


__all__ = [
    "ClearReadDenied",
    "ClearReadPrincipal",
    "PIIClearAuditLog",
    "PII_READ_CLEAR_SCOPE",
    "adapt_api_key",
    "read_clear",
]
