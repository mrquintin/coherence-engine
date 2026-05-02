"""service accounts and v2 api keys (Argon2id, scopes, rate limit)

Replaces the legacy ``fund_api_keys`` schema (SHA-256 hashed tokens with a
single ``role`` column) with the v2 model from prompt 28: Argon2id-hashed
keys, an explicit JSON ``scopes`` array, per-key rate limits, and an
optional ``service_account_id`` foreign key into the new
``fund_service_accounts`` table.

Migration strategy (best-effort):

1. The ``api_key_id`` column on ``fund_api_key_audit_events`` is set to
   NULL for any row pointing at a legacy key — those audit rows survive
   the migration but become orphaned (the historical action / actor /
   request_id / path remain queryable, just without an FK target).
2. The legacy ``fund_api_keys`` table is dropped. Legacy keys cannot be
   re-issued on the new hash algorithm: their plaintext is unrecoverable
   from a SHA-256 digest, so any extant operational keys MUST be re-
   issued post-migration via ``coherence-engine api-keys create`` (or
   the admin API). Operators are warned in
   ``docs/specs/api_keys_v2.md``.
3. ``fund_service_accounts`` is created.
4. ``fund_api_keys`` is recreated with the v2 schema. The legacy
   compatibility columns (``label``, ``role``, ``key_fingerprint``,
   ``is_active``) are preserved so existing admin UI / workflow / fund
   middleware code paths continue to function during the transition.

Revision ID: 20260425_000004
Revises: 20260425_000003
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000004"
down_revision = "20260425_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Orphan any audit events that reference a soon-to-be-dropped legacy key.
    op.execute(
        sa.text(
            "UPDATE fund_api_key_audit_events SET api_key_id = NULL "
            "WHERE api_key_id IN (SELECT id FROM fund_api_keys)"
        )
    )

    # 2. Drop the legacy fund_api_keys table.
    if dialect == "sqlite":
        op.drop_table("fund_api_keys")
    else:
        # On PG the audit-events FK targets fund_api_keys; drop with CASCADE
        # so the constraint is severed cleanly. We re-add the FK below.
        op.execute(sa.text("DROP TABLE IF EXISTS fund_api_keys CASCADE"))

    # 3. Create fund_service_accounts.
    op.create_table(
        "fund_service_accounts",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_fund_service_accounts_name",
        "fund_service_accounts",
        ["name"],
        unique=True,
    )

    # 4. Recreate fund_api_keys with the v2 schema.
    op.create_table(
        "fund_api_keys",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("service_account_id", sa.String(length=40), nullable=True),
        sa.Column("prefix", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        # Legacy compat columns retained for the transitional period.
        sa.Column("label", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="service"),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["service_account_id"], ["fund_service_accounts.id"]),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index(
        "ix_fund_api_keys_service_account_id",
        "fund_api_keys",
        ["service_account_id"],
        unique=False,
    )
    op.create_index("ix_fund_api_keys_prefix", "fund_api_keys", ["prefix"], unique=False)
    op.create_index("ix_fund_api_keys_role", "fund_api_keys", ["role"], unique=False)
    op.create_index(
        "ix_fund_api_keys_key_hash", "fund_api_keys", ["key_hash"], unique=False
    )
    op.create_index(
        "ix_fund_api_keys_key_fingerprint",
        "fund_api_keys",
        ["key_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_fund_api_keys_is_active", "fund_api_keys", ["is_active"], unique=False
    )

    # Re-establish FK from audit events → keys on dialects that needed an
    # explicit drop above. SQLite keeps embedded FK constraints with the
    # parent table definition, so no ALTER is needed there.
    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_fund_api_key_audit_events_api_key_id",
            "fund_api_key_audit_events",
            "fund_api_keys",
            ["api_key_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect != "sqlite":
        op.drop_constraint(
            "fk_fund_api_key_audit_events_api_key_id",
            "fund_api_key_audit_events",
            type_="foreignkey",
        )

    op.drop_index("ix_fund_api_keys_is_active", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_key_fingerprint", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_key_hash", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_role", table_name="fund_api_keys")
    op.drop_index("ix_fund_api_keys_prefix", table_name="fund_api_keys")
    op.drop_index(
        "ix_fund_api_keys_service_account_id", table_name="fund_api_keys"
    )

    if dialect == "sqlite":
        op.drop_table("fund_api_keys")
    else:
        op.execute(sa.text("DROP TABLE IF EXISTS fund_api_keys CASCADE"))

    op.drop_index("ix_fund_service_accounts_name", table_name="fund_service_accounts")
    op.drop_table("fund_service_accounts")

    # Recreate the legacy schema so an in-place rollback is non-destructive
    # to the audit-events FK target.
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
    op.create_index(
        "ix_fund_api_keys_key_hash", "fund_api_keys", ["key_hash"], unique=False
    )
    op.create_index(
        "ix_fund_api_keys_key_fingerprint",
        "fund_api_keys",
        ["key_fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_fund_api_keys_is_active", "fund_api_keys", ["is_active"], unique=False
    )

    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_fund_api_key_audit_events_api_key_id",
            "fund_api_key_audit_events",
            "fund_api_keys",
            ["api_key_id"],
            ["id"],
        )
