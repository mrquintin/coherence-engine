"""add fund_investors and fund_verification_records for accredited LP intake

Adds the LP-side identity table (``fund_investors``) and the per-attempt
accreditation-verification ledger (``fund_verification_records``) used by
the accredited investor verification adapter (prompt 26). Founders and the
application-scoring pipeline are unaffected.

Storage discipline: ``fund_verification_records`` stores only the SHA-256
hash of the uploaded evidence plus an opaque object-storage URI. Raw
evidence bytes never enter the database.

Revision ID: 20260425_000003
Revises: 20260425_000002
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000003"
down_revision = "20260425_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_investors",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("founder_user_id", sa.String(length=64), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("residence_country", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("investor_type", sa.String(length=32), nullable=False, server_default="individual"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="unverified"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_fund_investors_founder_user_id",
        "fund_investors",
        ["founder_user_id"],
        unique=True,
    )
    op.create_index(
        "ix_fund_investors_investor_type",
        "fund_investors",
        ["investor_type"],
        unique=False,
    )
    op.create_index(
        "ix_fund_investors_status",
        "fund_investors",
        ["status"],
        unique=False,
    )

    op.create_table(
        "fund_verification_records",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "investor_id",
            sa.String(length=40),
            sa.ForeignKey("fund_investors.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False, server_default="self_certified"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_uri", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("provider_reference", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_verification_records_investor_id",
        "fund_verification_records",
        ["investor_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_verification_records_provider",
        "fund_verification_records",
        ["provider"],
        unique=False,
    )
    op.create_index(
        "ix_fund_verification_records_status",
        "fund_verification_records",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_fund_verification_records_idempotency_key",
        "fund_verification_records",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_verification_records_idempotency_key",
        table_name="fund_verification_records",
    )
    op.drop_index(
        "ix_fund_verification_records_status",
        table_name="fund_verification_records",
    )
    op.drop_index(
        "ix_fund_verification_records_provider",
        table_name="fund_verification_records",
    )
    op.drop_index(
        "ix_fund_verification_records_investor_id",
        table_name="fund_verification_records",
    )
    op.drop_table("fund_verification_records")

    op.drop_index("ix_fund_investors_status", table_name="fund_investors")
    op.drop_index("ix_fund_investors_investor_type", table_name="fund_investors")
    op.drop_index("ix_fund_investors_founder_user_id", table_name="fund_investors")
    op.drop_table("fund_investors")
