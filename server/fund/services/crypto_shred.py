"""Crypto-shredding for per-row encryption keys (prompt 57).

"Shredding" a key means: zero out the stored key material and stamp
``shredded_at``. The encrypted blob in the original row is left
untouched but is mathematically unrecoverable -- any subsequent
:func:`per_row_encryption.decrypt` call against that ``key_id`` raises
:class:`KeyShreddedError`.

This module is intentionally narrow: it does not delete database rows,
update business-domain tables, or call into object storage. The
retention service (:mod:`server.fund.services.retention`) composes
shredding with tombstoning + ``redacted=True`` flips.

Idempotency: shredding an already-shredded key is a no-op (the second
call returns ``False``); shredding an unknown key raises
:class:`KeyNotFoundError` so a typo at the caller cannot silently fail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from coherence_engine.server.fund.services.per_row_encryption import (
    KeyNotFoundError,
)


_LOG = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def shred_key(db: Session, key_id: str) -> bool:
    """Zero the key material for ``key_id``; return True iff a key was shredded.

    The row is kept (with ``key_material_b64=""`` and a ``shredded_at``
    timestamp) so audit logs that reference the key_id stay
    interpretable. The row is never re-keyed -- once shredded, the
    key_id is permanently dead.
    """
    from coherence_engine.server.fund import models

    if not key_id:
        raise ValueError("key_id is required")
    row = db.get(models.EncryptionKey, key_id)
    if row is None:
        raise KeyNotFoundError(f"unknown key_id: {key_id!r}")
    if row.shredded_at is not None:
        return False
    row.key_material_b64 = ""
    row.shredded_at = _utc_now()
    db.add(row)
    db.flush()
    _LOG.info("crypto_shred key_id=%s shredded_at=%s", key_id, row.shredded_at.isoformat())
    return True


def is_shredded(db: Session, key_id: str) -> bool:
    """Return True iff the key has been shredded (or doesn't exist)."""
    from coherence_engine.server.fund import models

    row = db.get(models.EncryptionKey, key_id)
    if row is None:
        return True
    return row.shredded_at is not None or not row.key_material_b64
