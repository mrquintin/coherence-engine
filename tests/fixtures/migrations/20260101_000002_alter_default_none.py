"""fixture migration — uses op.alter_column(..., server_default=None)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260101_000002"
down_revision = "20260101_000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fixture_clean",
        sa.Column("flag", sa.String(length=8), nullable=False, server_default="x"),
    )
    op.alter_column("fixture_clean", "flag", server_default=None)


def downgrade() -> None:
    op.drop_column("fixture_clean", "flag")
