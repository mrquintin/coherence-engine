"""create fund backend tables

Revision ID: 20260408_000001
Revises:
Create Date: 2026-04-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260408_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_founders",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_founders_email", "fund_founders", ["email"], unique=False)

    op.create_table(
        "fund_applications",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("founder_id", sa.String(length=40), nullable=False),
        sa.Column("one_liner", sa.Text(), nullable=False),
        sa.Column("requested_check_usd", sa.Integer(), nullable=False),
        sa.Column("use_of_funds_summary", sa.Text(), nullable=False),
        sa.Column("preferred_channel", sa.String(length=32), nullable=False),
        sa.Column("domain_primary", sa.String(length=64), nullable=False),
        sa.Column("compliance_status", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["founder_id"], ["fund_founders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_applications_founder_id", "fund_applications", ["founder_id"], unique=False)
    op.create_index("ix_fund_applications_status", "fund_applications", ["status"], unique=False)

    op.create_table(
        "fund_interview_sessions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("application_id", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("locale", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["fund_applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_interview_sessions_application_id", "fund_interview_sessions", ["application_id"], unique=False)

    op.create_table(
        "fund_scoring_jobs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("application_id", sa.String(length=40), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["fund_applications.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_scoring_jobs_application_id", "fund_scoring_jobs", ["application_id"], unique=False)
    op.create_index("ix_fund_scoring_jobs_status", "fund_scoring_jobs", ["status"], unique=False)

    op.create_table(
        "fund_decisions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("application_id", sa.String(length=40), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("parameter_set_id", sa.String(length=64), nullable=False),
        sa.Column("threshold_required", sa.Float(), nullable=False),
        sa.Column("coherence_observed", sa.Float(), nullable=False),
        sa.Column("margin", sa.Float(), nullable=False),
        sa.Column("failed_gates_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["fund_applications.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id"),
    )
    op.create_index("ix_fund_decisions_application_id", "fund_decisions", ["application_id"], unique=False)
    op.create_index("ix_fund_decisions_decision", "fund_decisions", ["decision"], unique=False)

    op.create_table(
        "fund_escalation_packets",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("application_id", sa.String(length=40), nullable=False),
        sa.Column("decision_id", sa.String(length=40), nullable=False),
        sa.Column("partner_email", sa.String(length=255), nullable=False),
        sa.Column("packet_uri", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["fund_applications.id"]),
        sa.ForeignKeyConstraint(["decision_id"], ["fund_decisions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_escalation_packets_application_id", "fund_escalation_packets", ["application_id"], unique=False)
    op.create_index("ix_fund_escalation_packets_decision_id", "fund_escalation_packets", ["decision_id"], unique=False)

    op.create_table(
        "fund_event_outbox",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_version", sa.String(length=32), nullable=False),
        sa.Column("producer", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_event_outbox_event_type", "fund_event_outbox", ["event_type"], unique=False)
    op.create_index("ix_fund_event_outbox_trace_id", "fund_event_outbox", ["trace_id"], unique=False)
    op.create_index("ix_fund_event_outbox_idempotency_key", "fund_event_outbox", ["idempotency_key"], unique=False)
    op.create_index("ix_fund_event_outbox_status", "fund_event_outbox", ["status"], unique=False)

    op.create_table(
        "fund_idempotency_records",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_idempotency_records_endpoint", "fund_idempotency_records", ["endpoint"], unique=False)
    op.create_index("ix_fund_idempotency_records_idempotency_key", "fund_idempotency_records", ["idempotency_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_fund_idempotency_records_idempotency_key", table_name="fund_idempotency_records")
    op.drop_index("ix_fund_idempotency_records_endpoint", table_name="fund_idempotency_records")
    op.drop_table("fund_idempotency_records")

    op.drop_index("ix_fund_event_outbox_status", table_name="fund_event_outbox")
    op.drop_index("ix_fund_event_outbox_idempotency_key", table_name="fund_event_outbox")
    op.drop_index("ix_fund_event_outbox_trace_id", table_name="fund_event_outbox")
    op.drop_index("ix_fund_event_outbox_event_type", table_name="fund_event_outbox")
    op.drop_table("fund_event_outbox")

    op.drop_index("ix_fund_escalation_packets_decision_id", table_name="fund_escalation_packets")
    op.drop_index("ix_fund_escalation_packets_application_id", table_name="fund_escalation_packets")
    op.drop_table("fund_escalation_packets")

    op.drop_index("ix_fund_decisions_decision", table_name="fund_decisions")
    op.drop_index("ix_fund_decisions_application_id", table_name="fund_decisions")
    op.drop_table("fund_decisions")

    op.drop_index("ix_fund_scoring_jobs_status", table_name="fund_scoring_jobs")
    op.drop_index("ix_fund_scoring_jobs_application_id", table_name="fund_scoring_jobs")
    op.drop_table("fund_scoring_jobs")

    op.drop_index("ix_fund_interview_sessions_application_id", table_name="fund_interview_sessions")
    op.drop_table("fund_interview_sessions")

    op.drop_index("ix_fund_applications_status", table_name="fund_applications")
    op.drop_index("ix_fund_applications_founder_id", table_name="fund_applications")
    op.drop_table("fund_applications")

    op.drop_index("ix_fund_founders_email", table_name="fund_founders")
    op.drop_table("fund_founders")

