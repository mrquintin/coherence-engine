"""create fund_notification_log table

Adds the notification dispatch ledger consumed by the new
``server/fund/services/notifications.py`` service (prompt 14):

* One row per ``(application_id, template_id)`` pair.
* Idempotent on ``idempotency_key`` (unique index) — second-and-later
  dispatches with the same key reuse the existing row instead of
  re-sending.
* Records ``channel`` (``dry_run | smtp | ses | sendgrid``),
  ``recipient`` (resolved to-address), ``status``
  (``pending | sent | failed | suppressed``), an operator-readable
  ``error`` blob, and ``created_at`` / ``sent_at`` timestamps.
* MUST NOT store raw credentials or rendered bodies containing
  sensitive material (per prompt 14 prohibition; enforced at the
  service layer).

The migration is idempotent: the table + indexes are only created if
they do not already exist (handles bootstrapping via
``Base.metadata.create_all``). Downgrade drops the indexes and the
table.

Revision ID: 20260417_000005
Revises: 20260417_000004
Create Date: 2026-04-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_000005"
down_revision = "20260417_000004"
branch_labels = None
depends_on = None


_TABLE = "fund_notification_log"


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

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=40), primary_key=True),
            sa.Column(
                "application_id",
                sa.String(length=40),
                sa.ForeignKey("fund_applications.id"),
                nullable=False,
            ),
            sa.Column(
                "template_id", sa.String(length=64), nullable=False
            ),
            sa.Column(
                "channel",
                sa.String(length=32),
                nullable=False,
                server_default="dry_run",
            ),
            sa.Column(
                "recipient", sa.String(length=255), nullable=False, server_default=""
            ),
            sa.Column(
                "idempotency_key", sa.String(length=64), nullable=False
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            ),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        )

    indexes = _existing_indexes(bind, _TABLE)
    if "ix_fund_notification_log_application_id" not in indexes:
        op.create_index(
            "ix_fund_notification_log_application_id",
            _TABLE,
            ["application_id"],
            unique=False,
        )
    if "ix_fund_notification_log_template_id" not in indexes:
        op.create_index(
            "ix_fund_notification_log_template_id",
            _TABLE,
            ["template_id"],
            unique=False,
        )
    if "ix_fund_notification_log_channel" not in indexes:
        op.create_index(
            "ix_fund_notification_log_channel",
            _TABLE,
            ["channel"],
            unique=False,
        )
    if "ix_fund_notification_log_status" not in indexes:
        op.create_index(
            "ix_fund_notification_log_status",
            _TABLE,
            ["status"],
            unique=False,
        )
    if "ux_fund_notification_log_idempotency_key" not in indexes:
        op.create_index(
            "ux_fund_notification_log_idempotency_key",
            _TABLE,
            ["idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    if _TABLE not in tables:
        return

    indexes = _existing_indexes(bind, _TABLE)
    for idx in (
        "ux_fund_notification_log_idempotency_key",
        "ix_fund_notification_log_status",
        "ix_fund_notification_log_channel",
        "ix_fund_notification_log_template_id",
        "ix_fund_notification_log_application_id",
    ):
        if idx in indexes:
            op.drop_index(idx, table_name=_TABLE)
    op.drop_table(_TABLE)
