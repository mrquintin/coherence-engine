"""add fund_interview_chunks for browser WebRTC voice intake

Adds the per-chunk staging ledger backing the browser-mode founder
interview surface (prompt 39). One row per 5-second
``audio/webm; codecs=opus`` chunk uploaded directly to object
storage via a signed URL. At finalize time the rows are sorted by
``seq`` and the underlying blobs are stitched (ffmpeg concat) into
a single ``interviews/<session>/full.webm`` artifact.

Revision ID: 20260425_000007
Revises: 20260425_000006
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000007"
down_revision = "20260425_000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_interview_chunks",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=40),
            sa.ForeignKey("fund_interview_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("chunk_uri", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "chunk_sha256",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "content_type",
            sa.String(length=64),
            nullable=False,
            server_default="audio/webm",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="initiated",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "session_id", "seq", name="uq_fund_interview_chunks_session_seq"
        ),
    )
    op.create_index(
        "ix_fund_interview_chunks_session_id",
        "fund_interview_chunks",
        ["session_id"],
    )
    op.create_index(
        "ix_fund_interview_chunks_application_id",
        "fund_interview_chunks",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_interview_chunks_seq",
        "fund_interview_chunks",
        ["seq"],
    )
    op.create_index(
        "ix_fund_interview_chunks_status",
        "fund_interview_chunks",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_interview_chunks_status",
        table_name="fund_interview_chunks",
    )
    op.drop_index(
        "ix_fund_interview_chunks_seq",
        table_name="fund_interview_chunks",
    )
    op.drop_index(
        "ix_fund_interview_chunks_application_id",
        table_name="fund_interview_chunks",
    )
    op.drop_index(
        "ix_fund_interview_chunks_session_id",
        table_name="fund_interview_chunks",
    )
    op.drop_table("fund_interview_chunks")
