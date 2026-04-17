"""add kind + payload_json columns to fund_argument_artifacts

Adds a generic ``kind`` column (server default ``"generic"`` so existing rows
backfill cleanly) so the ``fund_argument_artifacts`` table can host both the
classic argument artifacts and the new reproducible ``decision_artifact`` rows
produced by ``server/fund/services/decision_artifact.py``. A nullable
``payload_json`` column is added in the same revision to carry the canonical
artifact bytes for non-argument artifact kinds, and ``scoring_job_id`` is
relaxed to nullable on dialects that support it so artifact rows can be
persisted independently of a scoring job.

If the columns are already present (e.g. on databases recreated directly from
the current ORM metadata via ``Base.metadata.create_all``), the upgrade is a
no-op for those columns and the downgrade remains rollback-symmetric.

Revision ID: 20260417_000002
Revises: 20260417_000001
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_000002"
down_revision = "20260417_000001"
branch_labels = None
depends_on = None


_TABLE = "fund_argument_artifacts"


def _existing_columns(bind) -> set[str]:
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def _existing_indexes(bind) -> set[str]:
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)
    indexes = _existing_indexes(bind)

    if "kind" not in cols:
        op.add_column(
            _TABLE,
            sa.Column(
                "kind",
                sa.String(length=64),
                nullable=False,
                server_default="generic",
            ),
        )
    if "ix_fund_argument_artifacts_kind" not in indexes and "kind" in (cols | {"kind"}):
        op.create_index(
            "ix_fund_argument_artifacts_kind",
            _TABLE,
            ["kind"],
            unique=False,
        )

    if "payload_json" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("payload_json", sa.Text(), nullable=True),
        )

    # Relax scoring_job_id to nullable so decision-artifact rows can be
    # persisted independently of a scoring job. SQLite cannot ALTER a column
    # constraint in-place; on SQLite the ORM-side ``nullable=True`` declaration
    # is sufficient because tests recreate the schema via
    # ``Base.metadata.create_all``.
    if bind.dialect.name != "sqlite" and "scoring_job_id" in cols:
        with op.batch_alter_table(_TABLE) as batch:
            batch.alter_column(
                "scoring_job_id",
                existing_type=sa.String(length=40),
                nullable=True,
            )


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind)
    indexes = _existing_indexes(bind)

    if bind.dialect.name != "sqlite" and "scoring_job_id" in cols:
        with op.batch_alter_table(_TABLE) as batch:
            batch.alter_column(
                "scoring_job_id",
                existing_type=sa.String(length=40),
                nullable=False,
            )

    # SQLite < 3.35 lacks DROP COLUMN; modern alembic + SQLite (>=3.35) support
    # it natively, otherwise batch_alter_table is required. Use batch on SQLite
    # to stay portable across CI runners.
    use_batch = bind.dialect.name == "sqlite"

    if use_batch:
        with op.batch_alter_table(_TABLE) as batch:
            if "ix_fund_argument_artifacts_kind" in indexes:
                batch.drop_index("ix_fund_argument_artifacts_kind")
            if "payload_json" in cols:
                batch.drop_column("payload_json")
            if "kind" in cols:
                batch.drop_column("kind")
    else:
        if "ix_fund_argument_artifacts_kind" in indexes:
            op.drop_index("ix_fund_argument_artifacts_kind", table_name=_TABLE)
        if "payload_json" in cols:
            op.drop_column(_TABLE, "payload_json")
        if "kind" in cols:
            op.drop_column(_TABLE, "kind")
