"""retention + erasure tables, key_id columns (prompt 57)

Adds:

* ``fund_encryption_keys`` -- per-row AES-256 keys that get crypto-
  shredded when the retention horizon hits the row.
* ``fund_erasure_requests`` -- GDPR / CCPA right-to-delete request
  lifecycle, including 30-day grace + audit-hold refusals.
* ``redacted`` / ``redacted_at`` / ``redaction_reason`` tombstone
  columns on ``fund_applications``, ``fund_interview_recordings``,
  ``fund_kyc_results``.
* ``*_key_id`` columns pointing into ``fund_encryption_keys``.

All new columns are nullable / default-false so the migration is safe
to run against a populated database without a backfill (per the
expand/backfill/contract pattern used elsewhere in this repo).

Revision ID: 20260425_000014
Revises: 20260425_000013
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000014"
down_revision = "20260425_000013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # New tables
    # ------------------------------------------------------------------
    op.create_table(
        "fund_encryption_keys",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("key_material_b64", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("shredded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shred_reason", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_fund_encryption_keys_created_at",
        "fund_encryption_keys",
        ["created_at"],
    )
    op.create_index(
        "ix_fund_encryption_keys_shredded_at",
        "fund_encryption_keys",
        ["shredded_at"],
    )

    op.create_table(
        "fund_erasure_requests",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column(
            "subject_type",
            sa.String(length=32),
            nullable=False,
            server_default="founder",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending_subject_request",
        ),
        sa.Column(
            "verification_token_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column("issued_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requested_by", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "immediate", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("classes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "refusal_reason", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "request_id", sa.String(length=80), nullable=False, server_default=""
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fund_erasure_requests_subject_id",
        "fund_erasure_requests",
        ["subject_id"],
    )
    op.create_index(
        "ix_fund_erasure_requests_subject_type",
        "fund_erasure_requests",
        ["subject_type"],
    )
    op.create_index(
        "ix_fund_erasure_requests_status",
        "fund_erasure_requests",
        ["status"],
    )
    op.create_index(
        "ix_fund_erasure_requests_request_id",
        "fund_erasure_requests",
        ["request_id"],
    )

    # ------------------------------------------------------------------
    # Per-row key_id + redaction tombstone columns
    # ------------------------------------------------------------------
    for table, key_col in (
        ("fund_applications", "transcript_key_id"),
        ("fund_interview_recordings", "recording_key_id"),
        ("fund_kyc_results", "evidence_key_id"),
    ):
        op.add_column(
            table,
            sa.Column(key_col, sa.String(length=64), nullable=True),
        )
        op.create_index(
            f"ix_{table}_{key_col}",
            table,
            [key_col],
        )
        op.add_column(
            table,
            sa.Column(
                "redacted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            table,
            sa.Column("redacted_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "redaction_reason", sa.String(length=64), nullable=True
            ),
        )
        op.create_index(
            f"ix_{table}_redacted",
            table,
            ["redacted"],
        )


def downgrade() -> None:
    for table, key_col in (
        ("fund_kyc_results", "evidence_key_id"),
        ("fund_interview_recordings", "recording_key_id"),
        ("fund_applications", "transcript_key_id"),
    ):
        op.drop_index(f"ix_{table}_redacted", table_name=table)
        op.drop_column(table, "redaction_reason")
        op.drop_column(table, "redacted_at")
        op.drop_column(table, "redacted")
        op.drop_index(f"ix_{table}_{key_col}", table_name=table)
        op.drop_column(table, key_col)

    op.drop_index(
        "ix_fund_erasure_requests_request_id", table_name="fund_erasure_requests"
    )
    op.drop_index(
        "ix_fund_erasure_requests_status", table_name="fund_erasure_requests"
    )
    op.drop_index(
        "ix_fund_erasure_requests_subject_type", table_name="fund_erasure_requests"
    )
    op.drop_index(
        "ix_fund_erasure_requests_subject_id", table_name="fund_erasure_requests"
    )
    op.drop_table("fund_erasure_requests")

    op.drop_index(
        "ix_fund_encryption_keys_shredded_at", table_name="fund_encryption_keys"
    )
    op.drop_index(
        "ix_fund_encryption_keys_created_at", table_name="fund_encryption_keys"
    )
    op.drop_table("fund_encryption_keys")
