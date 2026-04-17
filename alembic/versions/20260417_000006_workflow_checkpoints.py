"""create fund_workflow_runs + fund_workflow_steps tables

Adds checkpoint tables consumed by the new workflow orchestrator
(``server/fund/services/workflow.py``, prompt 15):

* ``fund_workflow_runs`` — one row per call to
  :func:`workflow.run_workflow` for an application. Tracks overall
  status, current step, started / finished timestamps, and an
  operator-readable error string on failure.
* ``fund_workflow_steps`` — per-stage checkpoint rows keyed by
  ``(workflow_run_id, name)`` (unique). Records
  ``input_digest`` (SHA-256 of canonical JSON of the stage's
  inputs — lets resume detect upstream tampering),
  ``output_digest``, status, timestamps, and error.

Neither table stores raw credentials, rendered notification bodies,
or any secret-bearing payload (per prompt 14/15 prohibitions).

The migration is idempotent: tables + indexes are only created if
they do not already exist (handles bootstrapping via
``Base.metadata.create_all``). Downgrade drops both tables in
reverse order.

Revision ID: 20260417_000006
Revises: 20260417_000005
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_000006"
down_revision = "20260417_000005"
branch_labels = None
depends_on = None


_RUNS = "fund_workflow_runs"
_STEPS = "fund_workflow_steps"


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _existing_indexes(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if _RUNS not in tables:
        op.create_table(
            _RUNS,
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column(
                "application_id",
                sa.String(length=40),
                sa.ForeignKey("fund_applications.id"),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "current_step",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
        )

    runs_indexes = _existing_indexes(bind, _RUNS)
    if "ix_fund_workflow_runs_application_id" not in runs_indexes:
        op.create_index(
            "ix_fund_workflow_runs_application_id",
            _RUNS,
            ["application_id"],
            unique=False,
        )
    if "ix_fund_workflow_runs_status" not in runs_indexes:
        op.create_index(
            "ix_fund_workflow_runs_status",
            _RUNS,
            ["status"],
            unique=False,
        )

    if _STEPS not in tables:
        op.create_table(
            _STEPS,
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column(
                "workflow_run_id",
                sa.String(length=40),
                sa.ForeignKey("fund_workflow_runs.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "input_digest",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "output_digest",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
        )

    steps_indexes = _existing_indexes(bind, _STEPS)
    if "ix_fund_workflow_steps_workflow_run_id" not in steps_indexes:
        op.create_index(
            "ix_fund_workflow_steps_workflow_run_id",
            _STEPS,
            ["workflow_run_id"],
            unique=False,
        )
    if "ix_fund_workflow_steps_name" not in steps_indexes:
        op.create_index(
            "ix_fund_workflow_steps_name",
            _STEPS,
            ["name"],
            unique=False,
        )
    if "ix_fund_workflow_steps_status" not in steps_indexes:
        op.create_index(
            "ix_fund_workflow_steps_status",
            _STEPS,
            ["status"],
            unique=False,
        )
    if "ux_fund_workflow_steps_run_name" not in steps_indexes:
        op.create_index(
            "ux_fund_workflow_steps_run_name",
            _STEPS,
            ["workflow_run_id", "name"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if _STEPS in tables:
        steps_indexes = _existing_indexes(bind, _STEPS)
        for idx in (
            "ux_fund_workflow_steps_run_name",
            "ix_fund_workflow_steps_status",
            "ix_fund_workflow_steps_name",
            "ix_fund_workflow_steps_workflow_run_id",
        ):
            if idx in steps_indexes:
                op.drop_index(idx, table_name=_STEPS)
        op.drop_table(_STEPS)

    if _RUNS in tables:
        runs_indexes = _existing_indexes(bind, _RUNS)
        for idx in (
            "ix_fund_workflow_runs_status",
            "ix_fund_workflow_runs_application_id",
        ):
            if idx in runs_indexes:
                op.drop_index(idx, table_name=_RUNS)
        op.drop_table(_RUNS)
