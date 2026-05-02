"""fixture migration — uses batch_alter_table (sqlite-only pattern)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260101_000004"
down_revision = "20260101_000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("fixture_bool") as batch:
        batch.add_column(sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("fixture_bool") as batch:
        batch.drop_column("note")
