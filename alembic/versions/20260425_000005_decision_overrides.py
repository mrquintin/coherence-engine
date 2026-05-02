"""add fund_decision_overrides for partner manual override flow

Adds the ledger backing the partner-dashboard override flow (prompt 35).
Each row is an operator action superseding the automated verdict on a
single application; the original ``fund_decisions`` row is never
mutated. Indexed on ``application_id`` (lookup the active override),
``override_verdict`` (filter the partner pipeline view), ``reason_code``
(audit roll-ups), ``status`` (active vs. superseded), and
``overridden_by`` (per-partner history).

Revision ID: 20260425_000005
Revises: 20260425_000004
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000005"
down_revision = "20260425_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_decision_overrides",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("original_verdict", sa.String(length=32), nullable=False),
        sa.Column("override_verdict", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=48), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("overridden_by", sa.String(length=128), nullable=False),
        sa.Column(
            "justification_uri",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_fund_decision_overrides_application_id",
        "fund_decision_overrides",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_decision_overrides_override_verdict",
        "fund_decision_overrides",
        ["override_verdict"],
    )
    op.create_index(
        "ix_fund_decision_overrides_reason_code",
        "fund_decision_overrides",
        ["reason_code"],
    )
    op.create_index(
        "ix_fund_decision_overrides_status",
        "fund_decision_overrides",
        ["status"],
    )
    op.create_index(
        "ix_fund_decision_overrides_overridden_by",
        "fund_decision_overrides",
        ["overridden_by"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_decision_overrides_overridden_by",
        table_name="fund_decision_overrides",
    )
    op.drop_index(
        "ix_fund_decision_overrides_status",
        table_name="fund_decision_overrides",
    )
    op.drop_index(
        "ix_fund_decision_overrides_reason_code",
        table_name="fund_decision_overrides",
    )
    op.drop_index(
        "ix_fund_decision_overrides_override_verdict",
        table_name="fund_decision_overrides",
    )
    op.drop_index(
        "ix_fund_decision_overrides_application_id",
        table_name="fund_decision_overrides",
    )
    op.drop_table("fund_decision_overrides")
