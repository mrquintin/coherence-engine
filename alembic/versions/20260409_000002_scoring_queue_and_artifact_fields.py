"""scoring queue and artifact fields

Revision ID: 20260409_000002
Revises: 20260408_000001
Create Date: 2026-04-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_000002"
down_revision = "20260408_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fund_applications", sa.Column("transcript_text", sa.Text(), nullable=True))
    op.add_column("fund_applications", sa.Column("transcript_uri", sa.Text(), nullable=True))
    op.add_column("fund_applications", sa.Column("argument_propositions_uri", sa.Text(), nullable=True))
    op.add_column("fund_applications", sa.Column("argument_relations_uri", sa.Text(), nullable=True))

    op.add_column("fund_scoring_jobs", sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""))
    op.add_column("fund_scoring_jobs", sa.Column("idempotency_key", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("fund_scoring_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("fund_scoring_jobs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("fund_scoring_jobs", sa.Column("error_message", sa.Text(), nullable=False, server_default=""))

    op.create_table(
        "fund_argument_artifacts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("application_id", sa.String(length=40), nullable=False),
        sa.Column("scoring_job_id", sa.String(length=40), nullable=False),
        sa.Column("propositions_json", sa.Text(), nullable=False),
        sa.Column("relations_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["fund_applications.id"]),
        sa.ForeignKeyConstraint(["scoring_job_id"], ["fund_scoring_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_argument_artifacts_application_id", "fund_argument_artifacts", ["application_id"], unique=False)
    op.create_index("ix_fund_argument_artifacts_scoring_job_id", "fund_argument_artifacts", ["scoring_job_id"], unique=False)

    op.alter_column("fund_scoring_jobs", "trace_id", server_default=None)
    op.alter_column("fund_scoring_jobs", "idempotency_key", server_default=None)
    op.alter_column("fund_scoring_jobs", "error_message", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_fund_argument_artifacts_scoring_job_id", table_name="fund_argument_artifacts")
    op.drop_index("ix_fund_argument_artifacts_application_id", table_name="fund_argument_artifacts")
    op.drop_table("fund_argument_artifacts")

    op.drop_column("fund_scoring_jobs", "error_message")
    op.drop_column("fund_scoring_jobs", "completed_at")
    op.drop_column("fund_scoring_jobs", "started_at")
    op.drop_column("fund_scoring_jobs", "idempotency_key")
    op.drop_column("fund_scoring_jobs", "trace_id")

    op.drop_column("fund_applications", "argument_relations_uri")
    op.drop_column("fund_applications", "argument_propositions_uri")
    op.drop_column("fund_applications", "transcript_uri")
    op.drop_column("fund_applications", "transcript_text")

