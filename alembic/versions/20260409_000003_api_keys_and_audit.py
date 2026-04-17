"""api keys and audit trail tables

Revision ID: 20260409_000003
Revises: 20260409_000002
Create Date: 2026-04-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_000003"
down_revision = "20260409_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_api_keys",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=24), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_fund_api_keys_role", "fund_api_keys", ["role"], unique=False)
    op.create_index("ix_fund_api_keys_key_hash", "fund_api_keys", ["key_hash"], unique=False)
    op.create_index("ix_fund_api_keys_key_fingerprint", "fund_api_keys", ["key_fingerprint"], unique=False)
    op.create_index("ix_fund_api_keys_is_active", "fund_api_keys", ["is_active"], unique=False)

    op.create_table(
        "fund_api_key_audit_events",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("api_key_id", sa.String(length=40), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("ip", sa.String(length=80), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["fund_api_keys.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_api_key_audit_events_api_key_id", "fund_api_key_audit_events", ["api_key_id"], unique=False)
    op.create_index("ix_fund_api_key_audit_events_action", "fund_api_key_audit_events", ["action"], unique=False)
    op.create_index("ix_fund_api_key_audit_events_success", "fund_api_key_audit_events", ["success"], unique=False)
    op.create_index("ix_fund_api_key_audit_events_request_id", "fund_api_key_audit_events", ["request_id"], unique=False)
    op.create_index("ix_fund_api_key_audit_events_created_at", "fund_api_key_audit_events", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_fund_api_key_audit_events_created_at", table_name="fund_api_key_audit_events")
    op.drop_index("ix_fund_api_key_audit_events_request_id", table_name="fund_api_key_audit_events")
    op.drop_index("ix_fund_api_key_audit_events_success", table_name="fund_api_key_audit_events")
    op.drop_index("ix_fund_api_key_audit_events_action", table_name="fund_api_key_audit_events")
    op.drop_index("ix_fund_api_key_audit_events_api_key_id", table_name="fund_api_key_audit_events")
    op.drop_table("fund_api_key_audit_events")

    op.drop_index("ix_fund_api_keys_is_active", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_key_fingerprint", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_key_hash", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_role", table_name="fund_api_keys")
    op.drop_table("fund_api_keys")

