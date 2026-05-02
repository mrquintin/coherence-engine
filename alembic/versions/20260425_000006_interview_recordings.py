"""add fund_interview_recordings for Twilio voice intake

Adds the per-topic recording ledger backing the phone-interview
ingress (prompt 38). One row per topic answered during a Twilio
``<Record>`` step; the recording binary itself lives in object
storage. The table stores metadata + a SHA-256 of the stored blob.

Revision ID: 20260425_000006
Revises: 20260425_000005
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000006"
down_revision = "20260425_000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_interview_recordings",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(length=40),
            sa.ForeignKey("fund_interview_sessions.id"),
            nullable=False,
        ),
        sa.Column("topic_id", sa.String(length=64), nullable=False),
        sa.Column("recording_uri", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "recording_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "duration_seconds",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "provider_recording_sid",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_interview_recordings_application_id",
        "fund_interview_recordings",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_interview_recordings_session_id",
        "fund_interview_recordings",
        ["session_id"],
    )
    op.create_index(
        "ix_fund_interview_recordings_topic_id",
        "fund_interview_recordings",
        ["topic_id"],
    )
    op.create_index(
        "ix_fund_interview_recordings_provider_recording_sid",
        "fund_interview_recordings",
        ["provider_recording_sid"],
    )
    op.create_index(
        "ix_fund_interview_recordings_status",
        "fund_interview_recordings",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_interview_recordings_status",
        table_name="fund_interview_recordings",
    )
    op.drop_index(
        "ix_fund_interview_recordings_provider_recording_sid",
        table_name="fund_interview_recordings",
    )
    op.drop_index(
        "ix_fund_interview_recordings_topic_id",
        table_name="fund_interview_recordings",
    )
    op.drop_index(
        "ix_fund_interview_recordings_session_id",
        table_name="fund_interview_recordings",
    )
    op.drop_index(
        "ix_fund_interview_recordings_application_id",
        table_name="fund_interview_recordings",
    )
    op.drop_table("fund_interview_recordings")
