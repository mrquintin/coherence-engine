"""add fund_interview_sessions.state_json for adaptive policy

Adds the per-session policy state blob backing the adaptive
interview engine (prompt 41). The column is an opaque JSON string
written by ``services/interview_policy.py`` and read by the
recovery flow in ``services/interview_recovery.py``. The schema
inside the blob is owned by the policy module — the database
treats it as plain text.

Default empty string ensures backfill is a no-op on existing rows
(empty == "no policy state yet"; the policy initialises lazily on
first use).

Revision ID: 20260425_000008
Revises: 20260425_000007
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000008"
down_revision = "20260425_000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fund_interview_sessions",
        sa.Column(
            "state_json",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("fund_interview_sessions", "state_json")
