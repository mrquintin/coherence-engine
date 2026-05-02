"""fixture migration — naked Boolean column with nullable=False, no default."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260101_000003"
down_revision = "20260101_000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fixture_bool",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("fixture_bool")
