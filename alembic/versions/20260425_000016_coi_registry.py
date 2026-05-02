"""conflict-of-interest registry: declarations + checks (prompt 59)

Adds two tables backing the automated COI gate that fires before any
partner meeting is auto-booked or a ``pass`` decision is finalized:

* ``fund_coi_declarations`` -- each row is a partner's standing
  declaration of a relationship that creates (or might create) a
  conflict on the applications they touch. ``period_start`` /
  ``period_end`` bound the validity window; an open-ended row uses
  ``period_end IS NULL`` to mean "still active".
* ``fund_coi_checks`` -- each row records the result of a single
  ``check_coi(application, partner)`` evaluation. The same
  ``(application_id, partner_id)`` pair can produce multiple rows
  over time (a re-check fires on every meeting proposal + decision
  finalization) so the table is append-only by convention.

Revision ID: 20260425_000016
Revises: 20260425_000015
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000016"
down_revision = "20260425_000015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_coi_declarations",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("partner_id", sa.String(length=128), nullable=False),
        sa.Column(
            "party_kind",
            sa.String(length=16),
            nullable=False,
            server_default="company",
        ),
        sa.Column("party_id_ref", sa.String(length=128), nullable=False),
        sa.Column("relationship", sa.String(length=32), nullable=False),
        sa.Column(
            "period_start",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "period_end",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("evidence_uri", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fund_coi_declarations_partner_id",
        "fund_coi_declarations",
        ["partner_id"],
    )
    op.create_index(
        "ix_fund_coi_declarations_party_id_ref",
        "fund_coi_declarations",
        ["party_id_ref"],
    )
    op.create_index(
        "ix_fund_coi_declarations_relationship",
        "fund_coi_declarations",
        ["relationship"],
    )
    op.create_index(
        "ix_fund_coi_declarations_status",
        "fund_coi_declarations",
        ["status"],
    )

    op.create_table(
        "fund_coi_checks",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("partner_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "disclosure_uri",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "override_id",
            sa.String(length=40),
            nullable=True,
        ),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fund_coi_checks_application_id",
        "fund_coi_checks",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_coi_checks_partner_id",
        "fund_coi_checks",
        ["partner_id"],
    )
    op.create_index(
        "ix_fund_coi_checks_status",
        "fund_coi_checks",
        ["status"],
    )

    op.create_table(
        "fund_coi_overrides",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("partner_id", sa.String(length=128), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("overridden_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fund_coi_overrides_application_id",
        "fund_coi_overrides",
        ["application_id"],
    )
    op.create_index(
        "ix_fund_coi_overrides_partner_id",
        "fund_coi_overrides",
        ["partner_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_coi_overrides_partner_id", table_name="fund_coi_overrides"
    )
    op.drop_index(
        "ix_fund_coi_overrides_application_id", table_name="fund_coi_overrides"
    )
    op.drop_table("fund_coi_overrides")

    op.drop_index("ix_fund_coi_checks_status", table_name="fund_coi_checks")
    op.drop_index("ix_fund_coi_checks_partner_id", table_name="fund_coi_checks")
    op.drop_index(
        "ix_fund_coi_checks_application_id", table_name="fund_coi_checks"
    )
    op.drop_table("fund_coi_checks")

    op.drop_index(
        "ix_fund_coi_declarations_status", table_name="fund_coi_declarations"
    )
    op.drop_index(
        "ix_fund_coi_declarations_relationship",
        table_name="fund_coi_declarations",
    )
    op.drop_index(
        "ix_fund_coi_declarations_party_id_ref",
        table_name="fund_coi_declarations",
    )
    op.drop_index(
        "ix_fund_coi_declarations_partner_id",
        table_name="fund_coi_declarations",
    )
    op.drop_table("fund_coi_declarations")
