"""create portfolio_state and positions tables

Adds two portfolio-level tables consumed by the decision policy's
``R(S, portfolio_state)`` terms:

* ``portfolio_state`` — append-only snapshots of fund NAV, liquidity reserve,
  drawdown proxy, and regime. The most recent row (by ``as_of``) is treated
  as the current state.
* ``positions`` — per-application invested USD rows aggregated by
  ``(domain, status)`` for domain-concentration computations.

Both tables are record-only: nothing in this migration (or in the repository
that reads from them) performs trades, transfers, or live ledger writes.

The migration is idempotent: tables are only created if they do not already
exist (e.g. when the schema was bootstrapped via
``Base.metadata.create_all``). Downgrade drops both tables.

Revision ID: 20260417_000003
Revises: 20260417_000002
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_000003"
down_revision = "20260417_000002"
branch_labels = None
depends_on = None


_PORTFOLIO_STATE = "portfolio_state"
_POSITIONS = "positions"


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

    if _PORTFOLIO_STATE not in tables:
        op.create_table(
            _PORTFOLIO_STATE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "as_of",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column("fund_nav_usd", sa.Float(), nullable=False, server_default="0"),
            sa.Column(
                "liquidity_reserve_usd", sa.Float(), nullable=False, server_default="0"
            ),
            sa.Column("drawdown_proxy", sa.Float(), nullable=False, server_default="0"),
            sa.Column(
                "regime",
                sa.String(length=32),
                nullable=False,
                server_default="normal",
            ),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
        )

    ps_indexes = _existing_indexes(bind, _PORTFOLIO_STATE)
    if "ix_portfolio_state_as_of" not in ps_indexes:
        op.create_index(
            "ix_portfolio_state_as_of", _PORTFOLIO_STATE, ["as_of"], unique=False
        )

    if _POSITIONS not in tables:
        op.create_table(
            _POSITIONS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "application_id", sa.String(length=40), nullable=False, index=True
            ),
            sa.Column("domain", sa.String(length=64), nullable=False, index=True),
            sa.Column(
                "invested_usd", sa.Float(), nullable=False, server_default="0"
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="active",
            ),
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

    pos_indexes = _existing_indexes(bind, _POSITIONS)
    if "ix_positions_application_id" not in pos_indexes:
        op.create_index(
            "ix_positions_application_id",
            _POSITIONS,
            ["application_id"],
            unique=False,
        )
    if "ix_positions_domain" not in pos_indexes:
        op.create_index(
            "ix_positions_domain", _POSITIONS, ["domain"], unique=False
        )
    if "ix_positions_status" not in pos_indexes:
        op.create_index(
            "ix_positions_status", _POSITIONS, ["status"], unique=False
        )
    if "ix_positions_domain_status" not in pos_indexes:
        op.create_index(
            "ix_positions_domain_status",
            _POSITIONS,
            ["domain", "status"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if _POSITIONS in tables:
        pos_indexes = _existing_indexes(bind, _POSITIONS)
        for idx in (
            "ix_positions_domain_status",
            "ix_positions_status",
            "ix_positions_domain",
            "ix_positions_application_id",
        ):
            if idx in pos_indexes:
                op.drop_index(idx, table_name=_POSITIONS)
        op.drop_table(_POSITIONS)

    if _PORTFOLIO_STATE in tables:
        ps_indexes = _existing_indexes(bind, _PORTFOLIO_STATE)
        if "ix_portfolio_state_as_of" in ps_indexes:
            op.drop_index("ix_portfolio_state_as_of", table_name=_PORTFOLIO_STATE)
        op.drop_table(_PORTFOLIO_STATE)
