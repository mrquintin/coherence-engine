"""add founder_user_id to fund_founders for Supabase Auth integration

Adds a nullable ``founder_user_id`` column on ``fund_founders`` carrying the
Supabase Auth ``sub`` claim, plus a unique index. Nullable per the
expand/backfill/contract rollout pattern from prompt 24 — pre-existing
founders are not associated with a Supabase user until they sign in via
the founder portal.

Revision ID: 20260425_000002
Revises: 20260417_000006
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000002"
down_revision = "20260417_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fund_founders",
        sa.Column("founder_user_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_fund_founders_founder_user_id",
        "fund_founders",
        ["founder_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_fund_founders_founder_user_id", table_name="fund_founders")
    op.drop_column("fund_founders", "founder_user_id")
