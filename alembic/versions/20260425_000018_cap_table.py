"""cap-table issuance ledger (prompt 68)

Adds the ``fund_cap_table_issuances`` table -- one row per issuance the
operator has caused via the upstream investment workflow (signed SAFE
+ sent investment instruction). The system records issuances; it
does NOT unilaterally issue securities. The Carta / Pulley provider
sync is record-keeping only and the row is the local source of
truth: a divergence between the local row and the provider response
is flagged by :class:`CapTableService.reconcile`, never auto-healed
by trusting the provider.

The unique ``idempotency_key`` index makes ``record_issuance`` safe
to call twice for the same logical issuance (webhook replay, retry).

Revision ID: 20260425_000018
Revises: 20260425_000017
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000018"
down_revision = "20260425_000017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_cap_table_issuances",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("instrument_type", sa.String(length=64), nullable=False),
        sa.Column("amount_usd", sa.Integer(), nullable=False),
        sa.Column(
            "valuation_cap_usd",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "discount",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "board_consent_uri",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "provider_issuance_id",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_fund_cap_table_issuances_application_id",
        "fund_cap_table_issuances",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_cap_table_issuances_status",
        "fund_cap_table_issuances",
        ["status"],
    )
    op.create_index(
        "ix_fund_cap_table_issuances_provider_issuance_id",
        "fund_cap_table_issuances",
        ["provider_issuance_id"],
    )
    op.create_index(
        "uq_fund_cap_table_issuances_idempotency_key",
        "fund_cap_table_issuances",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_fund_cap_table_issuances_idempotency_key",
        table_name="fund_cap_table_issuances",
    )
    op.drop_index(
        "ix_fund_cap_table_issuances_provider_issuance_id",
        table_name="fund_cap_table_issuances",
    )
    op.drop_index(
        "ix_fund_cap_table_issuances_status",
        table_name="fund_cap_table_issuances",
    )
    op.drop_index(
        "ix_fund_cap_table_issuances_application_id",
        table_name="fund_cap_table_issuances",
    )
    op.drop_table("fund_cap_table_issuances")
