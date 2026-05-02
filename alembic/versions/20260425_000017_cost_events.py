"""per-application cost telemetry (prompt 62)

Adds the ``fund_cost_events`` table -- one row per paid external call
(LLM tokens, STT minutes, embeddings, Twilio voice minutes, Stripe
fees). The unit count is computed server-side from the observed
input/output (NEVER trusted from the client) and the ``unit_cost_usd``
+ ``total_usd`` are derived from the operator-managed pricing table at
``data/governed/cost_pricing.yaml`` so a price change is a YAML edit,
not a code change.

The unique ``idempotency_key`` index makes ``record_cost`` safe to call
twice for the same logical event (e.g. a webhook retry) -- the second
write returns the existing row instead of double-billing.

Revision ID: 20260425_000017
Revises: 20260425_000016
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000017"
down_revision = "20260425_000016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_cost_events",
        sa.Column("id", sa.String(length=40), primary_key=True),
        # Nullable so cross-cutting infra cost (background workers,
        # baseline polling) can still be recorded without an
        # application id.
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=False),
        sa.Column("units", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "unit_cost_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fund_cost_events_application_id",
        "fund_cost_events",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_cost_events_provider",
        "fund_cost_events",
        ["provider"],
    )
    op.create_index(
        "ix_fund_cost_events_sku",
        "fund_cost_events",
        ["sku"],
    )
    op.create_index(
        "ix_fund_cost_events_occurred_at",
        "fund_cost_events",
        ["occurred_at"],
    )
    op.create_index(
        "uq_fund_cost_events_idempotency_key",
        "fund_cost_events",
        ["idempotency_key"],
        unique=True,
    )

    # Cooldown ledger for budget alerts -- one row per
    # (scope, scope_key) so we can suppress repeated alerts inside the
    # cooldown window without re-querying the outbox.
    op.create_table(
        "fund_cost_alert_state",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column(
            "last_alert_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_total_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index(
        "uq_fund_cost_alert_state_scope_key",
        "fund_cost_alert_state",
        ["scope", "scope_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_fund_cost_alert_state_scope_key",
        table_name="fund_cost_alert_state",
    )
    op.drop_table("fund_cost_alert_state")

    op.drop_index(
        "uq_fund_cost_events_idempotency_key",
        table_name="fund_cost_events",
    )
    op.drop_index(
        "ix_fund_cost_events_occurred_at", table_name="fund_cost_events"
    )
    op.drop_index("ix_fund_cost_events_sku", table_name="fund_cost_events")
    op.drop_index(
        "ix_fund_cost_events_provider", table_name="fund_cost_events"
    )
    op.drop_index(
        "ix_fund_cost_events_application_id", table_name="fund_cost_events"
    )
    op.drop_table("fund_cost_events")
