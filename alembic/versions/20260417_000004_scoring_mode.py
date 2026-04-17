"""add scoring_mode column to fund_applications

Introduces a per-application ``scoring_mode ∈ {"enforce", "shadow"}`` used by
the orchestrator to suppress founder/partner notification side effects while
still producing a ``shadow_decision_artifact`` row and a
``DecisionIssued`` outbox event tagged with ``mode = "shadow"``.

Idempotent upgrade: the column is added only if it does not already exist
(handles the case where the ORM bootstrapped the schema via
``Base.metadata.create_all``). Existing rows receive the default value
``"enforce"`` via ``server_default`` so we never surprise any in-flight
applications with a different behavior.

Revision ID: 20260417_000004
Revises: 20260417_000003
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_000004"
down_revision = "20260417_000003"
branch_labels = None
depends_on = None


_TABLE = "fund_applications"
_COLUMN = "scoring_mode"


def _existing_columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _existing_columns(bind, _TABLE)
    if _COLUMN in columns:
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(length=16),
            nullable=False,
            server_default="enforce",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = _existing_columns(bind, _TABLE)
    if _COLUMN not in columns:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
