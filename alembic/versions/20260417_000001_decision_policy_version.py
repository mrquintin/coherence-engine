"""decision policy version column on fund_decisions

Revision ID: 20260417_000001
Revises: 20260409_000004
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_000001"
down_revision = "20260409_000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fund_decisions",
        sa.Column("decision_policy_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fund_decisions", "decision_policy_version")
