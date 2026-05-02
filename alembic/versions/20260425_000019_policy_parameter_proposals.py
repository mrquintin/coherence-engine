"""policy parameter proposals + approvals (prompt 70)

Adds the persistence tables for the reserve-allocation optimizer
(prompt 70) proposal lifecycle:

* ``fund_policy_parameter_proposals`` -- one row per operator-submitted
  proposal produced by ``reserve_optimizer.optimize``. The blob in
  ``parameters_json`` is the canonical
  ``OptimizerResult.to_canonical_dict()`` bytes the operator reviewed,
  stored verbatim so the audit trail does not depend on a downstream
  rendering pass.
* ``fund_policy_parameter_approvals`` -- append-only ledger of
  approve/reject transitions, keyed back to the proposal row.

The optimizer never auto-promotes; rows are inserted in ``proposed``
status and require an explicit admin transition to ``approved`` before
any downstream consumer is allowed to promote the parameters into the
running decision policy.

Revision ID: 20260425_000019
Revises: 20260425_000018
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000019"
down_revision = "20260425_000018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_policy_parameter_proposals",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "proposed_by",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column(
            "rationale",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "backtest_report_uri",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="proposed",
        ),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
        "ix_fund_policy_parameter_proposals_status",
        "fund_policy_parameter_proposals",
        ["status"],
    )
    op.create_index(
        "ix_fund_policy_parameter_proposals_created_at",
        "fund_policy_parameter_proposals",
        ["created_at"],
    )

    op.create_table(
        "fund_policy_parameter_approvals",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(length=40),
            sa.ForeignKey("fund_policy_parameter_proposals.id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column(
            "note",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_fund_policy_parameter_approvals_proposal_id",
        "fund_policy_parameter_approvals",
        ["proposal_id"],
    )
    op.create_index(
        "ix_fund_policy_parameter_approvals_decision",
        "fund_policy_parameter_approvals",
        ["decision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_policy_parameter_approvals_decision",
        table_name="fund_policy_parameter_approvals",
    )
    op.drop_index(
        "ix_fund_policy_parameter_approvals_proposal_id",
        table_name="fund_policy_parameter_approvals",
    )
    op.drop_table("fund_policy_parameter_approvals")
    op.drop_index(
        "ix_fund_policy_parameter_proposals_created_at",
        table_name="fund_policy_parameter_proposals",
    )
    op.drop_index(
        "ix_fund_policy_parameter_proposals_status",
        table_name="fund_policy_parameter_proposals",
    )
    op.drop_table("fund_policy_parameter_proposals")
