"""worker reliability fields for scoring/outbox

Revision ID: 20260409_000004
Revises: 20260409_000003
Create Date: 2026-04-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_000004"
down_revision = "20260409_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fund_scoring_jobs",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "fund_scoring_jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
    )
    op.add_column(
        "fund_scoring_jobs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "fund_scoring_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "fund_scoring_jobs",
        sa.Column("locked_by", sa.String(length=128), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_fund_scoring_jobs_next_attempt_at",
        "fund_scoring_jobs",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_fund_scoring_jobs_lease_expires_at",
        "fund_scoring_jobs",
        ["lease_expires_at"],
        unique=False,
    )

    op.add_column(
        "fund_event_outbox",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_event_outbox_next_attempt_at",
        "fund_event_outbox",
        ["next_attempt_at"],
        unique=False,
    )

    op.alter_column("fund_scoring_jobs", "attempts", server_default=None)
    op.alter_column("fund_scoring_jobs", "max_attempts", server_default=None)
    op.alter_column("fund_scoring_jobs", "locked_by", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_fund_event_outbox_next_attempt_at", table_name="fund_event_outbox")
    op.drop_column("fund_event_outbox", "next_attempt_at")

    op.drop_index("ix_fund_scoring_jobs_lease_expires_at", table_name="fund_scoring_jobs")
    op.drop_index("ix_fund_scoring_jobs_next_attempt_at", table_name="fund_scoring_jobs")
    op.drop_column("fund_scoring_jobs", "locked_by")
    op.drop_column("fund_scoring_jobs", "lease_expires_at")
    op.drop_column("fund_scoring_jobs", "next_attempt_at")
    op.drop_column("fund_scoring_jobs", "max_attempts")
    op.drop_column("fund_scoring_jobs", "attempts")
