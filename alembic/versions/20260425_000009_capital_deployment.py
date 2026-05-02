"""add fund_investment_instructions and fund_treasurer_approvals

Adds the capital-deployment ledger (prompt 51): the
``fund_investment_instructions`` table records the prepared transfer
intent and its lifecycle (``prepared`` -> ``approved`` -> ``sent`` |
``failed`` | ``cancelled``); the ``fund_treasurer_approvals`` table
captures every human approval so dual-approval and audit can be
reconstructed from the database alone.

Storage discipline: ``target_account_ref`` is an opaque token issued
by the upstream PSP (Stripe Connect account id, Mercury counterparty
id). Raw bank account / routing numbers never enter the database --
the bank-API verification step happens out-of-band and only the
provider token is persisted.

Revision ID: 20260425_000009
Revises: 20260425_000008
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000009"
down_revision = "20260425_000008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_investment_instructions",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column(
            "founder_id",
            sa.String(length=40),
            sa.ForeignKey("fund_founders.id"),
            nullable=False,
        ),
        sa.Column("amount_usd", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("target_account_ref", sa.String(length=255), nullable=False),
        sa.Column(
            "preparation_method",
            sa.String(length=32),
            nullable=False,
            server_default="bank_transfer",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="prepared",
        ),
        sa.Column("provider_intent_ref", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("prepared_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("treasurer_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_investment_instructions_application_id",
        "fund_investment_instructions",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_investment_instructions_status",
        "fund_investment_instructions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_fund_investment_instructions_idempotency_key",
        "fund_investment_instructions",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "fund_treasurer_approvals",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "instruction_id",
            sa.String(length=40),
            sa.ForeignKey("fund_investment_instructions.id"),
            nullable=False,
        ),
        sa.Column("treasurer_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False, server_default="approve"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_fund_treasurer_approvals_instruction_id",
        "fund_treasurer_approvals",
        ["instruction_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_treasurer_approvals_treasurer_id",
        "fund_treasurer_approvals",
        ["treasurer_id"],
        unique=False,
    )
    op.create_index(
        "uq_fund_treasurer_approvals_instruction_treasurer",
        "fund_treasurer_approvals",
        ["instruction_id", "treasurer_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_fund_treasurer_approvals_instruction_treasurer",
        table_name="fund_treasurer_approvals",
    )
    op.drop_index(
        "ix_fund_treasurer_approvals_treasurer_id",
        table_name="fund_treasurer_approvals",
    )
    op.drop_index(
        "ix_fund_treasurer_approvals_instruction_id",
        table_name="fund_treasurer_approvals",
    )
    op.drop_table("fund_treasurer_approvals")

    op.drop_index(
        "ix_fund_investment_instructions_idempotency_key",
        table_name="fund_investment_instructions",
    )
    op.drop_index(
        "ix_fund_investment_instructions_status",
        table_name="fund_investment_instructions",
    )
    op.drop_index(
        "ix_fund_investment_instructions_application_id",
        table_name="fund_investment_instructions",
    )
    op.drop_table("fund_investment_instructions")
