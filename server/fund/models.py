"""SQLAlchemy ORM models for fund backend."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from coherence_engine.server.fund.database import Base
from coherence_engine.server.fund.services.decision_policy import DECISION_POLICY_VERSION


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Founder(Base):
    __tablename__ = "fund_founders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    applications = relationship("Application", back_populates="founder")


class Application(Base):
    __tablename__ = "fund_applications"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    founder_id: Mapped[str] = mapped_column(ForeignKey("fund_founders.id"), index=True)
    one_liner: Mapped[str] = mapped_column(Text)
    requested_check_usd: Mapped[int] = mapped_column(Integer)
    use_of_funds_summary: Mapped[str] = mapped_column(Text)
    preferred_channel: Mapped[str] = mapped_column(String(32))
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    argument_propositions_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    argument_relations_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_primary: Mapped[str] = mapped_column(String(64), default="market_economics")
    compliance_status: Mapped[str] = mapped_column(String(32), default="clear")
    status: Mapped[str] = mapped_column(String(64), default="intake_created", index=True)
    # Per-application scoring mode. ``enforce`` = production behavior (current);
    # ``shadow`` = pipeline runs scoring + builds a ``shadow_decision_artifact``
    # and emits a ``DecisionIssued`` event tagged with ``mode="shadow"`` but
    # suppresses founder/partner notification side effects. See prompt 12.
    scoring_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="enforce", default="enforce"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    founder = relationship("Founder", back_populates="applications")
    interview_sessions = relationship("InterviewSession", back_populates="application")
    scoring_jobs = relationship("ScoringJob", back_populates="application")
    argument_artifacts = relationship("ArgumentArtifact", back_populates="application")
    decision = relationship("Decision", back_populates="application", uselist=False)


class InterviewSession(Base):
    __tablename__ = "fund_interview_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("fund_applications.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    locale: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    application = relationship("Application", back_populates="interview_sessions")


class ScoringJob(Base):
    __tablename__ = "fund_scoring_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("fund_applications.id"), index=True)
    mode: Mapped[str] = mapped_column(String(32))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    trace_id: Mapped[str] = mapped_column(String(80), default="")
    idempotency_key: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")

    application = relationship("Application", back_populates="scoring_jobs")
    artifacts = relationship("ArgumentArtifact", back_populates="scoring_job")


class ArgumentArtifact(Base):
    __tablename__ = "fund_argument_artifacts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("fund_applications.id"), index=True)
    scoring_job_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("fund_scoring_jobs.id"), index=True, nullable=True
    )
    propositions_json: Mapped[str] = mapped_column(Text, default="[]")
    relations_json: Mapped[str] = mapped_column(Text, default="[]")
    kind: Mapped[str] = mapped_column(String(64), default="generic", index=True)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    application = relationship("Application", back_populates="argument_artifacts")
    scoring_job = relationship("ScoringJob", back_populates="artifacts")


class Decision(Base):
    __tablename__ = "fund_decisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("fund_applications.id"), unique=True, index=True)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    policy_version: Mapped[str] = mapped_column(String(64))
    decision_policy_version: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, default=DECISION_POLICY_VERSION
    )
    parameter_set_id: Mapped[str] = mapped_column(String(64))
    threshold_required: Mapped[float] = mapped_column(Float)
    coherence_observed: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    failed_gates_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    application = relationship("Application", back_populates="decision")


class EscalationPacket(Base):
    __tablename__ = "fund_escalation_packets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("fund_applications.id"), index=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("fund_decisions.id"), index=True)
    partner_email: Mapped[str] = mapped_column(String(255))
    packet_uri: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="sent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class EventOutbox(Base):
    __tablename__ = "fund_event_outbox"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    event_version: Mapped[str] = mapped_column(String(32))
    producer: Mapped[str] = mapped_column(String(128))
    trace_id: Mapped[str] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyRecord(Base):
    __tablename__ = "fund_idempotency_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class NotificationLog(Base):
    """Idempotent notification dispatch ledger (prompt 14).

    One row per ``(application_id, template_id)`` pair. Writes are
    idempotent on ``idempotency_key = sha256(application_id|template_id)``
    enforced by a unique index; second-and-later dispatches with the same
    key reuse the existing row instead of re-sending.

    The table records *what* was dispatched and *to whom* (channel +
    recipient address). It MUST NOT store raw credentials, secrets,
    rendered template bodies that contain sensitive material, or any
    backend-specific auth artifacts (per prompt 14 prohibition). The
    ``error`` column captures structured failure reasons (operator-
    readable strings) but never credentials or stack traces with
    secrets.
    """

    __tablename__ = "fund_notification_log"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    template_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="dry_run", index=True)
    recipient: Mapped[str] = mapped_column(String(255), default="")
    idempotency_key: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowRun(Base):
    """Per-application workflow orchestration run (prompt 15).

    One row per invocation of
    :func:`coherence_engine.server.fund.services.workflow.run_workflow`.
    Tracks overall status (``pending | running | succeeded | failed``),
    the name of the currently-executing (or last-failing) step, wall-
    clock start / finish timestamps, and a short operator-readable
    error string on failure.

    The row is idempotency-scoped to ``application_id``: a resume reuses
    the most recent non-succeeded row for the same application; a fresh
    ``run`` starts a new row. Nothing in this table holds raw
    credentials, rendered notification bodies, or any secret-bearing
    payload — operator-readable state only (per prompts 14 + 15).
    """

    __tablename__ = "fund_workflow_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_step: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class WorkflowStep(Base):
    """Per-stage checkpoint row for a :class:`WorkflowRun` (prompt 15).

    One row per ``(workflow_run_id, name)`` pair — enforced by a unique
    index — so a resume can locate the exact checkpoint for any stage.
    ``input_digest`` is the SHA-256 of the canonical JSON of the inputs
    the stage consumed; it lets resume detect upstream tampering. A
    resume against a succeeded step whose recomputed ``input_digest``
    diverges from the stored value refuses without ``--force``.

    Statuses: ``pending | running | succeeded | failed | skipped``.
    """

    __tablename__ = "fund_workflow_steps"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("fund_workflow_runs.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_digest: Mapped[str] = mapped_column(String(64), default="")
    output_digest: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class ApiKey(Base):
    __tablename__ = "fund_api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key_fingerprint: Mapped[str] = mapped_column(String(24), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)


class PortfolioState(Base):
    """Snapshot of portfolio-level capacity, reserves, and regime.

    Rows are immutable append-only records; the "current" state is the row
    with the largest ``as_of``. Writes go through
    :class:`coherence_engine.server.fund.repositories.portfolio_repository.PortfolioRepository`
    which does not mutate existing rows and does not perform any live ledger
    or transfer operations (see prompt 10 prohibitions).
    """

    __tablename__ = "portfolio_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)
    fund_nav_usd: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_reserve_usd: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_proxy: Mapped[float] = mapped_column(Float, default=0.0)
    regime: Mapped[str] = mapped_column(String(32), default="normal")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class Position(Base):
    """Record-only position entry used for domain-concentration aggregation."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(String(40), index=True)
    domain: Mapped[str] = mapped_column(String(64), index=True)
    invested_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)


class ApiKeyAuditEvent(Base):
    __tablename__ = "fund_api_key_audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("fund_api_keys.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    actor: Mapped[str] = mapped_column(String(255), default="")
    request_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    ip: Mapped[str] = mapped_column(String(80), default="")
    path: Mapped[str] = mapped_column(String(255), default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)

