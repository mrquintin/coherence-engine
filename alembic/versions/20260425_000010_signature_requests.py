"""add fund_signature_requests

Adds the e-signature ledger (prompt 52). One row per
``(application, document_template, idempotency_key)`` triple records
the lifecycle of a SAFE / term-sheet signature request through one of
the pluggable provider backends (DocuSign, Dropbox Sign).

Storage discipline: the unsigned document body is never persisted --
``template_vars_hash`` is the sha256 of the rendered template
variables, which together with ``document_template`` uniquely
identifies the document body without storing it. The signed PDF
returned by the provider is uploaded to object storage and the
``coh://`` URI is stored in ``signed_pdf_uri``.

Revision ID: 20260425_000010
Revises: 20260425_000009
Create Date: 2026-04-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260425_000010"
down_revision = "20260425_000009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_signature_requests",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("fund_applications.id"),
            nullable=False,
        ),
        sa.Column("document_template", sa.String(length=128), nullable=False),
        sa.Column(
            "template_vars_hash",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "provider_request_id",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="prepared",
        ),
        sa.Column("signed_pdf_uri", sa.Text(), nullable=False, server_default=""),
        sa.Column("signers_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_signature_requests_application_id",
        "fund_signature_requests",
        ["application_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_signature_requests_status",
        "fund_signature_requests",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_fund_signature_requests_provider_request_id",
        "fund_signature_requests",
        ["provider_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_fund_signature_requests_idempotency_key",
        "fund_signature_requests",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fund_signature_requests_idempotency_key",
        table_name="fund_signature_requests",
    )
    op.drop_index(
        "ix_fund_signature_requests_provider_request_id",
        table_name="fund_signature_requests",
    )
    op.drop_index(
        "ix_fund_signature_requests_status",
        table_name="fund_signature_requests",
    )
    op.drop_index(
        "ix_fund_signature_requests_application_id",
        table_name="fund_signature_requests",
    )
    op.drop_table("fund_signature_requests")
