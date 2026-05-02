"""PII tokenization columns + clear-read audit log (prompt 58)

Adds:

* ``email_token``, ``email_clear``, ``email_clear_key_id`` columns on
  ``fund_founders``. ``email_token`` is the deterministic HMAC-SHA-256
  token (see :mod:`pii_tokenization`); ``email_clear`` is the
  per-row AES-GCM ciphertext of the clear email under the key
  identified by ``email_clear_key_id``.
* ``pii_clear_audit_log`` table — append-only record of every clear-PII
  read. Tampering protection lives in the RLS policies installed
  separately by :mod:`server.fund.security.rls`
  (``PII_AUDIT_RLS_POLICIES``); this migration also installs them on
  Postgres.

Backfill strategy
-----------------

For each existing ``fund_founders`` row with a non-empty ``email``:

1. Compute the token via :func:`pii_tokenization.tokenize` and write
   it to ``email_token``.
2. Mint a per-row AES-GCM key, encrypt the clear email under it, and
   write ``email_clear`` + ``email_clear_key_id``.

The legacy ``email`` column is left in place during this expand /
backfill phase. The contract phase (drop ``email``) is a separate
prompt once all callers are switched to ``email_token`` +
``read_clear_email``.

Backfill is best-effort: rows whose backfill fails (e.g. missing
``PII_TENANT_SALT`` in the running env) are left with ``NULL``
tokenization columns and re-attempted on the next run. The migration
itself succeeds either way -- a missing salt is an operator
configuration issue, not a schema issue.

Revision ID: 20260425_000015
Revises: 20260425_000014
Create Date: 2026-04-25
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op


revision = "20260425_000015"
down_revision = "20260425_000014"
branch_labels = None
depends_on = None


_log = logging.getLogger(__name__)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Tokenization + clear-cipher columns on fund_founders
    # ------------------------------------------------------------------
    op.add_column(
        "fund_founders",
        sa.Column("email_token", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_fund_founders_email_token",
        "fund_founders",
        ["email_token"],
    )
    op.add_column(
        "fund_founders",
        sa.Column("email_clear", sa.Text(), nullable=True),
    )
    op.add_column(
        "fund_founders",
        sa.Column("email_clear_key_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_fund_founders_email_clear_key_id",
        "fund_founders",
        ["email_clear_key_id"],
    )

    # ------------------------------------------------------------------
    # pii_clear_audit_log -- INSERT-only audit table
    # ------------------------------------------------------------------
    op.create_table(
        "pii_clear_audit_log",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("principal_id", sa.String(length=128), nullable=False),
        sa.Column(
            "principal_kind",
            sa.String(length=32),
            nullable=False,
            server_default="api_key",
        ),
        sa.Column("field_kind", sa.String(length=32), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("subject_table", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column(
            "route", sa.String(length=255), nullable=False, server_default=""
        ),
        sa.Column(
            "request_id", sa.String(length=80), nullable=False, server_default=""
        ),
        sa.Column(
            "reason", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pii_clear_audit_log_principal_id",
        "pii_clear_audit_log",
        ["principal_id"],
    )
    op.create_index(
        "ix_pii_clear_audit_log_field_kind",
        "pii_clear_audit_log",
        ["field_kind"],
    )
    op.create_index(
        "ix_pii_clear_audit_log_token",
        "pii_clear_audit_log",
        ["token"],
    )
    op.create_index(
        "ix_pii_clear_audit_log_subject_id",
        "pii_clear_audit_log",
        ["subject_id"],
    )
    op.create_index(
        "ix_pii_clear_audit_log_request_id",
        "pii_clear_audit_log",
        ["request_id"],
    )
    op.create_index(
        "ix_pii_clear_audit_log_created_at",
        "pii_clear_audit_log",
        ["created_at"],
    )

    bind = op.get_bind()
    dialect = bind.dialect.name.lower()

    # ------------------------------------------------------------------
    # Postgres-only: install RLS for the audit table.
    # ------------------------------------------------------------------
    if dialect == "postgresql":
        from coherence_engine.server.fund.security.rls import (
            PII_AUDIT_RLS_POLICIES,
            apply_rls_policies,
        )

        apply_rls_policies(bind, PII_AUDIT_RLS_POLICIES)

        # Defence-in-depth: a transition trigger that raises on any
        # UPDATE / DELETE attempt, even from a superuser bypassing RLS.
        bind.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION pii_clear_audit_log_immutable()
                RETURNS trigger AS $$
                BEGIN
                  RAISE EXCEPTION
                    'pii_clear_audit_log is INSERT-only (op=%)', TG_OP;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        bind.execute(
            sa.text(
                """
                DROP TRIGGER IF EXISTS pii_clear_audit_log_no_update
                ON public.pii_clear_audit_log;
                """
            )
        )
        bind.execute(
            sa.text(
                """
                CREATE TRIGGER pii_clear_audit_log_no_update
                BEFORE UPDATE OR DELETE ON public.pii_clear_audit_log
                FOR EACH ROW EXECUTE FUNCTION pii_clear_audit_log_immutable();
                """
            )
        )

    # ------------------------------------------------------------------
    # Best-effort backfill of email_token / email_clear / key_id.
    # ------------------------------------------------------------------
    try:
        from coherence_engine.server.fund.services import (
            per_row_encryption,
            pii_tokenization,
        )
    except Exception as exc:  # pragma: no cover - import safety net
        _log.warning("pii backfill skipped: %s", exc)
        return

    rows = bind.execute(
        sa.text(
            "SELECT id, email FROM fund_founders "
            "WHERE email IS NOT NULL AND email != '' "
            "AND (email_token IS NULL OR email_token = '')"
        )
    ).fetchall()

    if not rows:
        return

    # Encryption needs an ORM Session for the key store; build one
    # bound to this migration's connection.
    from sqlalchemy.orm import Session

    session = Session(bind=bind)
    try:
        for row in rows:
            founder_id = row[0]
            clear_email = row[1]
            try:
                token = pii_tokenization.tokenize(clear_email, kind="email")
            except pii_tokenization.PIITokenizationError as exc:
                _log.warning(
                    "pii backfill: skipping founder %s -- no salt: %s",
                    founder_id,
                    exc,
                )
                # No salt available -- bail out of the whole loop, no
                # point trying every row.
                break
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning(
                    "pii backfill: tokenize failed for founder %s: %s",
                    founder_id,
                    exc,
                )
                continue

            try:
                key_id, ciphertext = per_row_encryption.encrypt(
                    clear_email.encode("utf-8"),
                    db=session,
                    row_id=founder_id,
                )
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning(
                    "pii backfill: encrypt failed for founder %s: %s",
                    founder_id,
                    exc,
                )
                continue

            bind.execute(
                sa.text(
                    "UPDATE fund_founders "
                    "SET email_token = :tok, "
                    "    email_clear = :ct, "
                    "    email_clear_key_id = :kid "
                    "WHERE id = :id"
                ),
                {
                    "tok": token,
                    "ct": ciphertext,
                    "kid": key_id,
                    "id": founder_id,
                },
            )
        session.flush()
    finally:
        session.close()


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name.lower()

    if dialect == "postgresql":
        bind.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS pii_clear_audit_log_no_update "
                "ON public.pii_clear_audit_log;"
            )
        )
        bind.execute(
            sa.text("DROP FUNCTION IF EXISTS pii_clear_audit_log_immutable();")
        )

        from coherence_engine.server.fund.security.rls import (
            PII_AUDIT_RLS_POLICIES,
            revert_rls_policies,
        )

        revert_rls_policies(bind, PII_AUDIT_RLS_POLICIES)

    op.drop_index(
        "ix_pii_clear_audit_log_created_at", table_name="pii_clear_audit_log"
    )
    op.drop_index(
        "ix_pii_clear_audit_log_request_id", table_name="pii_clear_audit_log"
    )
    op.drop_index(
        "ix_pii_clear_audit_log_subject_id", table_name="pii_clear_audit_log"
    )
    op.drop_index(
        "ix_pii_clear_audit_log_token", table_name="pii_clear_audit_log"
    )
    op.drop_index(
        "ix_pii_clear_audit_log_field_kind", table_name="pii_clear_audit_log"
    )
    op.drop_index(
        "ix_pii_clear_audit_log_principal_id", table_name="pii_clear_audit_log"
    )
    op.drop_table("pii_clear_audit_log")

    op.drop_index(
        "ix_fund_founders_email_clear_key_id", table_name="fund_founders"
    )
    op.drop_column("fund_founders", "email_clear_key_id")
    op.drop_column("fund_founders", "email_clear")
    op.drop_index("ix_fund_founders_email_token", table_name="fund_founders")
    op.drop_column("fund_founders", "email_token")
