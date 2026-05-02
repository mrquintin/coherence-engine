"""add fund_meeting_proposals + fund_bookings for partner-meeting scheduling (prompt 54)

Adds the partner-meeting scheduling ledger consumed by
``Scheduler`` (``server/fund/services/scheduler.py``). When an
enforce-mode ``pass`` decision is issued ``ApplicationService`` emits
a scheduling event and the scheduler queries the configured backend
(Cal.com primary, Google Calendar fallback) for availability,
proposes the top three slots to the founder, and on a token click-
through books the chosen slot and records a Booking row.

Storage discipline: the proposed-slot list is kept as a JSON-encoded
text blob (Text, NOT JSONB) so the same migration runs unmodified
against the SQLite test fixture and the Postgres staging/prod
clusters. Provider-side calendar event identifiers are persisted as
opaque strings; raw Cal.com / Google Calendar payloads never enter
the database.

Revision ID: 20260425_000012
Revises: 20260425_000011
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000012"
down_revision = "20260425_000011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_meeting_proposals",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("partner_email", sa.String(length=255), nullable=False),
        sa.Column(
            "founder_email",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column("duration_min", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "proposed_slots_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("backend", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_meeting_proposals_application_id",
        "fund_meeting_proposals",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_meeting_proposals_token",
        "fund_meeting_proposals",
        ["token"],
        unique=True,
    )
    op.create_index(
        "ix_fund_meeting_proposals_status",
        "fund_meeting_proposals",
        ["status"],
        unique=False,
    )

    op.create_table(
        "fund_bookings",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(length=40),
            sa.ForeignKey("fund_meeting_proposals.id"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("backend", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "provider_event_id",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column("partner_email", sa.String(length=255), nullable=False),
        sa.Column("founder_email", sa.String(length=255), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="confirmed",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_fund_bookings_proposal_id",
        "fund_bookings",
        ["proposal_id"],
        unique=True,
    )
    op.create_index(
        "ix_fund_bookings_application_id",
        "fund_bookings",
        ["application_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fund_bookings_application_id", table_name="fund_bookings")
    op.drop_index("ix_fund_bookings_proposal_id", table_name="fund_bookings")
    op.drop_table("fund_bookings")
    op.drop_index(
        "ix_fund_meeting_proposals_status",
        table_name="fund_meeting_proposals",
    )
    op.drop_index(
        "ix_fund_meeting_proposals_token",
        table_name="fund_meeting_proposals",
    )
    op.drop_index(
        "ix_fund_meeting_proposals_application_id",
        table_name="fund_meeting_proposals",
    )
    op.drop_table("fund_meeting_proposals")
