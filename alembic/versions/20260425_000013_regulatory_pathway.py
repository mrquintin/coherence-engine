"""add regulatory_pathway_id to fund_applications (prompt 56)

Adds a nullable ``regulatory_pathway_id`` column on
``fund_applications`` carrying the operator-resolved securities
pathway id (one of the ``id`` values declared in
``data/governed/regulatory_pathways.yaml``: ``reg_d_506b``,
``reg_d_506c``, ``reg_cf``, ``reg_s``).

Nullable per the expand/backfill/contract pattern -- pre-existing
applications have no resolved pathway until the classifier runs.
The decision-policy gate (``regulatory_pathway_clear``) does NOT
infer a pathway when the column is null; ambiguity routes to
``manual_review`` per prompt 56's "do NOT silently default to a
pathway" prohibition.

Revision ID: 20260425_000013
Revises: 20260425_000012
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000013"
down_revision = "20260425_000012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fund_applications",
        sa.Column("regulatory_pathway_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_fund_applications_regulatory_pathway_id",
        "fund_applications",
        ["regulatory_pathway_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_applications_regulatory_pathway_id",
        table_name="fund_applications",
    )
    op.drop_column("fund_applications", "regulatory_pathway_id")
