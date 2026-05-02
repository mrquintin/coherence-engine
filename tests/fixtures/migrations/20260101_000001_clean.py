"""clean fixture migration — no parity issues."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260101_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fixture_clean",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("fixture_clean")
