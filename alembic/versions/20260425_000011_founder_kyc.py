"""add fund_kyc_results for founder KYC/AML screening (prompt 53)

Adds the founder-side KYC/AML ledger. Distinct from
``fund_verification_records`` (prompt 26), which gates LP / investor
accreditation under SEC Rule 501. This table gates founder identity
under sanctions / PEP / AML / ID screening and is mandatory before
any capital instruction is issued against an application.

Storage discipline: only the SHA-256 hash of the evidence and the
provider's opaque evidence reference are persisted. Raw KYC document
content -- passport scans, utility bills, sanctions-payload bytes --
never enters the database.

Revision ID: 20260425_000011
Revises: 20260425_000010
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000011"
down_revision = "20260425_000010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_kyc_results",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "founder_id",
            sa.String(length=40),
            sa.ForeignKey("fund_founders.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "screening_categories",
            sa.String(length=128),
            nullable=False,
            server_default="sanctions,pep,id,aml",
        ),
        sa.Column("evidence_uri", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "evidence_hash",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "provider_reference",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "error_code",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "failure_reason",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "refresh_required_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_index(
        "ix_fund_kyc_results_founder_id",
        "fund_kyc_results",
        ["founder_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_kyc_results_provider",
        "fund_kyc_results",
        ["provider"],
        unique=False,
    )
    op.create_index(
        "ix_fund_kyc_results_status",
        "fund_kyc_results",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_fund_kyc_results_idempotency_key",
        "fund_kyc_results",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_kyc_results_idempotency_key",
        table_name="fund_kyc_results",
    )
    op.drop_index("ix_fund_kyc_results_status", table_name="fund_kyc_results")
    op.drop_index(
        "ix_fund_kyc_results_provider", table_name="fund_kyc_results"
    )
    op.drop_index(
        "ix_fund_kyc_results_founder_id", table_name="fund_kyc_results"
    )
    op.drop_table("fund_kyc_results")
