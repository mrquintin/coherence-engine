"""install Row-Level-Security policies on Supabase / Postgres

Reads the declarative policy registry in
``coherence_engine.server.fund.security.rls`` and emits, on Postgres only:

* ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` per protected table.
* ``DROP POLICY IF EXISTS`` + ``CREATE POLICY`` per declared policy.

On SQLite this migration is a no-op — SQLite has no RLS concept and the
default-dev experience must remain ``python -m pytest`` against the local
SQLite file.

Downgrade drops the policies and disables RLS, again Postgres-only.

Revision ID: 20260425_000001
Revises: 20260425_000002
Create Date: 2026-04-25
"""

from __future__ import annotations

from alembic import op


revision = "20260425_000001"
down_revision = "20260425_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name.lower() != "postgresql":
        # SQLite (and other non-Postgres dialects) have no RLS — no-op.
        return

    # Imported here so SQLite-only test paths don't pay the import cost
    # before the dialect check runs.
    from coherence_engine.server.fund.security.rls import (
        RLS_POLICIES,
        apply_rls_policies,
    )

    apply_rls_policies(bind, RLS_POLICIES)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name.lower() != "postgresql":
        return

    from coherence_engine.server.fund.security.rls import (
        RLS_POLICIES,
        revert_rls_policies,
    )

    revert_rls_policies(bind, RLS_POLICIES)
