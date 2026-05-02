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
    # Supabase Auth ``sub`` claim. Nullable during the expand/backfill/contract
    # rollout (prompt 24): pre-existing founders have no Supabase identity
    # until they sign in via the founder portal. Unique once set.
    founder_user_id: Mapped[Optional[str]] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # Tokenized form of ``email`` (prompt 58). Deterministic per tenant
    # via :mod:`pii_tokenization`. Going forward this is the column
    # that downstream systems (CRM, dedup, analytics) join on; the
    # legacy ``email`` column is retained for the expand/backfill phase
    # and will be dropped in the contract migration once every caller
    # has been moved to ``email_token`` + ``read_clear_email``.
    email_token: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    # Per-row AES-GCM ciphertext of the clear email (prompt 57 / 58).
    # Reachable only through :func:`read_clear_email`, which gates on
    # the ``pii:read_clear`` scope and writes a ``PIIClearAuditLog``
    # row per access.
    email_clear: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_clear_key_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    applications = relationship("Application", back_populates="founder")

    def read_clear_email(
        self,
        *,
        db,
        principal,
        route: str = "",
        request_id: str = "",
        reason: str = "",
    ) -> str:
        """Return the clear email after a scope check + audit-log write.

        ``principal`` is a
        :class:`~coherence_engine.server.fund.services.pii_clear_audit.ClearReadPrincipal`
        (or anything with an ``id``, ``kind``, and ``has_scope`` interface).
        Raises :class:`ClearReadDenied` when the scope check fails — the
        caller (router / service) is responsible for mapping that to
        HTTP 403.

        The audit row is flushed on ``db`` but not committed; the
        surrounding transaction owns commit so a rollback on the
        request path also rolls back the audit entry, preserving the
        invariant that an audit row exists iff the caller actually
        observed the clear value.
        """
        # Local import: pii_clear_audit imports from this module
        # transitively via Base, so importing at module scope would
        # create a circular import on first model load.
        from coherence_engine.server.fund.services.pii_clear_audit import (
            read_clear,
        )

        return read_clear(
            db=db,
            principal=principal,
            field_kind="email",
            token=self.email_token or "",
            ciphertext_b64=self.email_clear or "",
            key_id=self.email_clear_key_id or "",
            subject_table=self.__tablename__,
            subject_id=self.id,
            route=route,
            request_id=request_id,
            reason=reason,
        )


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
    # Per-row encryption key id (prompt 57). Points at
    # ``fund_encryption_keys.id``; the per-row AES-GCM key is shredded
    # when the transcript class hits its retention horizon. Nullable
    # for legacy rows that pre-date the encryption rollout.
    transcript_key_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # GDPR / retention tombstone marker (prompt 57). Set to True when
    # the daily retention job has tombstoned the transcript URI and
    # shredded the per-row key. Read endpoints MUST surface this as
    # HTTP 410 Gone with the redaction reason rather than returning
    # the (now meaningless) ciphertext.
    redacted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redaction_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
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
    # Operator-resolved regulatory pathway (prompt 56). Stable id from
    # ``data/governed/regulatory_pathways.yaml`` (e.g. ``reg_d_506c``).
    # Nullable: applications without a resolved pathway are routed to
    # ``manual_review`` by ``decision_policy`` -- the column is the
    # persisted resolution, not the gate itself.
    regulatory_pathway_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
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
    # Adaptive-policy state (prompt 41). JSON blob persisted per-session
    # so a dropped Twilio call can be resumed within 24h from
    # ``state.next_question``. See ``services/interview_policy.py``
    # for the schema; the column is opaque to the database.
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
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


class ServiceAccount(Base):
    """Service account that owns one or more :class:`ApiKey` rows (prompt 28).

    Service accounts represent non-human callers (workers, ingestion
    daemons, partner-side automation). One service account may hold
    multiple keys to support overlapping rotation windows. The unique
    ``name`` is the operator-readable handle used in the CLI / admin
    UI; ``owner_email`` records who is on the hook for rotating the
    keys when they near expiry.
    """

    __tablename__ = "fund_service_accounts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_email: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )

    api_keys = relationship("ApiKey", back_populates="service_account")


class ApiKey(Base):
    """API key with hashed-at-rest credential, explicit scopes, and rotation metadata (prompt 28).

    ``key_hash`` stores the **Argon2id**-encoded hash of the full presented
    token (never SHA-256). The plaintext token is only ever exposed once
    at creation/rotation time and is never persisted. Lookups go by
    ``prefix`` (the 8-char public discriminator embedded in the token);
    Argon2id verification is then performed in constant time against
    the matching row's ``key_hash``.

    Legacy columns (``label``, ``role``, ``key_fingerprint``,
    ``is_active``) are retained as compatibility shims so the existing
    admin UI / workflow router / middleware continue to operate while
    callers migrate to the v2 scope-based model. New keys mirror
    ``key_fingerprint = prefix`` and derive ``role`` from a coarse
    grouping of their scopes.
    """

    __tablename__ = "fund_api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    service_account_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("fund_service_accounts.id"), nullable=True, index=True
    )
    prefix: Mapped[str] = mapped_column(String(16), index=True, default="")
    scopes_json: Mapped[str] = mapped_column(Text, default="[]")
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)

    # Legacy compatibility columns — populated for new keys so admin UI /
    # middleware / workflow router that still read the old shape keep
    # working until they're migrated to ``security.api_key_auth``.
    label: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), index=True, default="service")
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    key_fingerprint: Mapped[str] = mapped_column(String(24), index=True, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, onupdate=_utc_now)

    service_account = relationship("ServiceAccount", back_populates="api_keys")


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


class Investor(Base):
    """LP / capital-source identity gated by accreditation verification (prompt 26).

    An ``Investor`` is the LP-side counterpart to ``Founder``: it
    represents whoever is putting capital *into* the fund, not the
    founders applying *for* capital. The fund's intake of LP commitments
    is gated on a successful :class:`VerificationRecord` for the
    investor (Rule 501 / accredited-investor verification). Founders
    are unaffected by this gate — the application-scoring pipeline
    runs identically regardless of investor verification state.

    The ``founder_user_id`` field carries the Supabase Auth ``sub``
    claim of the *human signed in to manage this investor profile*.
    For an individual investor that is the investor themself; for an
    entity it is the operator / authorized officer who manages the
    LP commitment on the entity's behalf. Naming mirrors
    ``Founder.founder_user_id`` for parallel auth-dependency wiring.
    """

    __tablename__ = "fund_investors"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    founder_user_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    legal_name: Mapped[str] = mapped_column(String(255), default="")
    residence_country: Mapped[str] = mapped_column(String(8), default="")
    investor_type: Mapped[str] = mapped_column(
        String(32), default="individual", index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="unverified", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )

    verification_records = relationship(
        "VerificationRecord", back_populates="investor"
    )


class VerificationRecord(Base):
    """Single accredited-investor verification attempt (prompt 26).

    One row per verification *attempt*. The current state of an
    investor is the most recent row by ``created_at``. A successful
    row is valid until ``expires_at`` (90 days per the SEC re-
    verification convention documented in
    ``docs/specs/accredited_investor_verification.md``); after that
    a fresh attempt is required.

    Storage discipline (prompt 26 prohibition): the table holds only
    the SHA-256 hash of the evidence payload plus an opaque
    object-storage URI (e.g. ``s3://...`` or
    ``supabase-storage://...``). Raw evidence bytes — uploaded W-2s,
    brokerage statements, attorney letters — never enter the database.
    """

    __tablename__ = "fund_verification_records"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    investor_id: Mapped[str] = mapped_column(
        ForeignKey("fund_investors.id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    method: Mapped[str] = mapped_column(String(64), default="self_certified")
    status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_uri: Mapped[str] = mapped_column(Text, default="")
    evidence_hash: Mapped[str] = mapped_column(String(64), default="")
    provider_reference: Mapped[str] = mapped_column(String(255), default="")
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    investor = relationship("Investor", back_populates="verification_records")


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


class DecisionOverride(Base):
    """Partner / admin manual override of an automated decision (prompt 35).

    Each row captures a single operator action that supersedes the
    machine verdict on an application. The underlying ``Decision`` row
    is never mutated; instead the most recent override row (per
    ``application_id``) is the system-of-record verdict.

    Idempotency / unrevise contract:

    * Only one *active* override per application. A second override
      attempt returns the existing row unless the caller passes
      ``unrevise=True`` (server-side ``--unrevise`` flag), in which case
      the prior row is marked ``superseded`` and the new row written.
    * ``reason_text`` is required and must be at least 40 characters so
      the reviewer cannot bypass the audit trail with a one-word
      justification (prompt 35 prohibition).
    * Every successful write emits a ``decision_overridden.v1`` outbox
      event so downstream consumers (founder notification, partner
      portfolio view) react to the new verdict.
    """

    __tablename__ = "fund_decision_overrides"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    original_verdict: Mapped[str] = mapped_column(String(32))
    override_verdict: Mapped[str] = mapped_column(String(32), index=True)
    reason_code: Mapped[str] = mapped_column(String(48), index=True)
    reason_text: Mapped[str] = mapped_column(Text)
    overridden_by: Mapped[str] = mapped_column(String(128), index=True)
    justification_uri: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="active", index=True
    )
    overridden_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class InterviewRecording(Base):
    """Single per-topic recording captured during a phone interview (prompt 38).

    One row per topic answered during a Twilio voice call. Recording
    bytes themselves live in object storage (``recording_uri``); the
    table holds only metadata + the SHA-256 of the stored blob so a
    later integrity check can detect tampering or partial uploads.
    """

    __tablename__ = "fund_interview_recordings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("fund_interview_sessions.id"), index=True
    )
    topic_id: Mapped[str] = mapped_column(String(64), index=True)
    recording_uri: Mapped[str] = mapped_column(Text, default="")
    recording_sha256: Mapped[str] = mapped_column(String(64), default="")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    provider_recording_sid: Mapped[str] = mapped_column(
        String(64), default="", index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-row encryption key id for the recording blob (prompt 57).
    recording_key_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    redacted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redaction_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )


class InvestmentInstruction(Base):
    """Prepared capital-deployment intent (prompt 51).

    A row is created when an authorized partner *prepares* a transfer
    against a funded application. The row is **inert** until a
    ``treasurer`` explicitly approves and then executes it. The
    software never moves money on its own; the lifecycle is

        prepared --(treasurer approve)--> approved --(treasurer execute)--> sent

    with terminal failure / cancel branches recorded on ``status``.

    Storage discipline (prompt 51 prohibition): ``target_account_ref``
    is an opaque token issued by the upstream PSP (Stripe Connect
    account id, Mercury counterparty id). Raw bank account / routing
    numbers are never persisted -- the bank-API verification step
    happens out-of-band and only the resulting provider token enters
    the database.
    """

    __tablename__ = "fund_investment_instructions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    founder_id: Mapped[str] = mapped_column(ForeignKey("fund_founders.id"))
    amount_usd: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    target_account_ref: Mapped[str] = mapped_column(String(255))
    preparation_method: Mapped[str] = mapped_column(
        String(32), default="bank_transfer"
    )
    status: Mapped[str] = mapped_column(
        String(32), default="prepared", index=True
    )
    provider_intent_ref: Mapped[str] = mapped_column(String(255), default="")
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    prepared_by: Mapped[str] = mapped_column(String(128), default="")
    treasurer_id: Mapped[str] = mapped_column(String(128), default="")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    approvals = relationship(
        "TreasurerApproval", back_populates="instruction"
    )


class TreasurerApproval(Base):
    """Single human approval against an :class:`InvestmentInstruction`.

    One row per (instruction, treasurer) pair -- the unique constraint
    in migration ``20260425_000009`` rejects duplicate sign-offs from
    the same operator. Dual-approval is implemented in the service
    layer by counting distinct rows with ``decision == "approve"``.
    """

    __tablename__ = "fund_treasurer_approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    instruction_id: Mapped[str] = mapped_column(
        ForeignKey("fund_investment_instructions.id"), index=True
    )
    treasurer_id: Mapped[str] = mapped_column(String(128), index=True)
    decision: Mapped[str] = mapped_column(String(16), default="approve")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )

    instruction = relationship(
        "InvestmentInstruction", back_populates="approvals"
    )


class InterviewChunk(Base):
    """A single 5-second WebRTC audio chunk uploaded by the browser (prompt 39).

    Browser-mode interviews stream chunks of ``audio/webm; codecs=opus``
    via signed URLs to object storage; one row per stored chunk. The
    server enforces monotonic, gap-free ``seq`` per session — the
    client cannot fabricate a sequence number out of order. At
    finalize time the rows are sorted by ``seq`` and the underlying
    blobs are stitched (ffmpeg concat) into a single
    ``interviews/<session>/full.webm`` artifact.

    Storage discipline: this table never holds audio bytes — only
    metadata + SHA-256 of the chunk payload. Hash drift between the
    expected payload and the stored blob is fatal (the chunk is
    rejected and the client must retry).
    """

    __tablename__ = "fund_interview_chunks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("fund_interview_sessions.id"), index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, index=True)
    chunk_uri: Mapped[str] = mapped_column(Text, default="")
    chunk_sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(
        String(64), default="audio/webm"
    )
    status: Mapped[str] = mapped_column(
        String(32), default="initiated", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SignatureRequest(Base):
    """E-signature request for a SAFE / term-sheet document (prompt 52).

    One row per ``(application, document_template, idempotency_key)``
    triple. Lifecycle:

        prepared --(send)--> sent --(provider webhook)--> signed
                                             |--> declined
                                             |--> expired
                                             |--> voided

    Storage discipline: the unsigned document body is rendered from the
    Jinja2 template at send time and immediately discarded -- only the
    template id + a sha256 of the rendered ``template_vars`` is
    persisted, so reproducing the exact document body requires the
    template + the original variables. The signed PDF returned by the
    provider is uploaded to object storage and the resulting
    ``coh://`` URI is stored in ``signed_pdf_uri``.

    Operator obligation: the SAFE template that ships with the repo is
    a placeholder. Production templates MUST be reviewed by securities
    counsel before any signature request is sent. This software does
    not provide legal advice -- see ``docs/specs/esignature.md``.
    """

    __tablename__ = "fund_signature_requests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    document_template: Mapped[str] = mapped_column(String(128))
    template_vars_hash: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(32))
    provider_request_id: Mapped[str] = mapped_column(
        String(255), default="", index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="prepared", index=True
    )
    signed_pdf_uri: Mapped[str] = mapped_column(Text, default="")
    signers_json: Mapped[str] = mapped_column(Text, default="[]")
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class KYCResult(Base):
    """Founder KYC/AML screening result (prompt 53).

    Distinct from :class:`VerificationRecord` (prompt 26): that table
    gates LP/investor accreditation under SEC Rule 501. *This* table
    gates founder-side KYC/AML — sanctions screening, PEP screening, ID
    verification — and is mandatory before any capital instruction is
    issued against an application. Decision-policy treats a missing or
    expired ``passed`` KYC as a downgrade of any ``pass`` verdict to
    ``manual_review`` with reason ``KYC_REQUIRED`` (see
    :mod:`server.fund.services.decision_policy`).

    Lifecycle:

        pending --(provider webhook)--> passed
                               |--> failed
                               |--> expired   (lazy, evaluated by service)

    A ``passed`` row is valid until ``expires_at`` (annual cadence per
    industry KYC convention; see ``docs/specs/founder_kyc.md``). The
    daily refresh job emits a ``founder_kyc.refresh_due`` event 30 days
    before expiry so the operator UI can prompt the founder to
    re-verify before any new funding event.

    Storage discipline (prompt 53 prohibition): the table holds only the
    SHA-256 hash of the document evidence plus the provider's opaque
    evidence reference (Persona inquiry id, Onfido check id, or an
    object-storage URI). Raw KYC document content — passport scans,
    utility bills, screening payloads — never enters the database.
    """

    __tablename__ = "fund_kyc_results"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    founder_id: Mapped[str] = mapped_column(
        ForeignKey("fund_founders.id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True
    )
    screening_categories: Mapped[str] = mapped_column(
        String(128), default="sanctions,pep,id,aml"
    )
    evidence_uri: Mapped[str] = mapped_column(Text, default="")
    evidence_hash: Mapped[str] = mapped_column(String(64), default="")
    provider_reference: Mapped[str] = mapped_column(String(255), default="")
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    error_code: Mapped[str] = mapped_column(String(64), default="")
    failure_reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_required_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-row encryption key id for the evidence URI (prompt 57). The
    # underlying provider-stored evidence (Persona / Onfido) is opaque
    # to us; the key here protects the URI + provider_reference fields
    # so a shred renders the pointer unrecoverable.
    evidence_key_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    redacted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redaction_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )


class MeetingProposal(Base):
    """Partner-meeting scheduling proposal (prompt 54).

    Created when an enforce-mode ``pass`` decision is issued. The
    scheduler queries the configured backend (Cal.com primary,
    Google Calendar fallback) for partner availability and proposes
    the top three slots to the founder. The ``token`` is the opaque
    handle the founder click-through carries; on book the proposal
    transitions to ``booked`` and a sibling :class:`Booking` row is
    written. Tokens past ``expires_at`` are rejected by the booking
    route with HTTP 410 Gone.

    Lifecycle::

        pending --(founder picks slot)--> booked
                |--(token expires)------> expired (lazy)
                |--(operator cancel)----> cancelled
    """

    __tablename__ = "fund_meeting_proposals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    partner_email: Mapped[str] = mapped_column(String(255))
    founder_email: Mapped[str] = mapped_column(String(255), default="")
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    # JSON-encoded list of three ISO-8601 datetimes (UTC). Stored as
    # Text rather than JSONB so the SQLite test fixture and Postgres
    # production schema share one definition.
    proposed_slots_json: Mapped[str] = mapped_column(Text, default="[]")
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    backend: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    booked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Booking(Base):
    """Confirmed partner-meeting booking (prompt 54).

    One booking per :class:`MeetingProposal`; ``proposal_id`` is
    unique. ``provider_event_id`` is the opaque identifier returned
    by the calendar backend (Cal.com booking id or Google Calendar
    event id) and is what the rescheduling/cancellation routes
    target. Raw provider payloads are NOT persisted here.
    """

    __tablename__ = "fund_bookings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("fund_meeting_proposals.id"), unique=True, index=True
    )
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    backend: Mapped[str] = mapped_column(String(32), default="")
    provider_event_id: Mapped[str] = mapped_column(String(255), default="")
    partner_email: Mapped[str] = mapped_column(String(255))
    founder_email: Mapped[str] = mapped_column(String(255))
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class EncryptionKey(Base):
    """Per-row AES-256-GCM key, crypto-shreddable (prompt 57).

    Each high-PII row (transcript, recording, KYC evidence) carries a
    ``*_key_id`` foreign reference into this table. The actual AES-256
    key bytes live in ``key_material_b64`` (32 raw bytes, base64-
    encoded). When the retention job or an explicit erasure request
    fires, :func:`crypto_shred.shred_key` zeroes ``key_material_b64``
    and stamps ``shredded_at``. The ciphertext on the original row is
    untouched but unrecoverable; this is the "crypto-shredding"
    technique that lets us comply with GDPR Art. 17 / CCPA right-to-
    delete without rewriting every encrypted blob.

    Storage discipline: this table holds key material in plaintext at
    rest only when no KMS is wired. Production deployments inject a
    KMS-backed :class:`EncryptionKeyStore` and ``key_material_b64``
    becomes a KMS handle rather than raw bytes.
    """

    __tablename__ = "fund_encryption_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_material_b64: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, index=True
    )
    shredded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    shred_reason: Mapped[str] = mapped_column(String(64), default="")


class ErasureRequest(Base):
    """GDPR / CCPA right-to-delete request lifecycle (prompt 57).

    Two-phase workflow:

    1. Support staff verify the requestor's identity out-of-band and
       run ``cli privacy issue-erasure-token --subject ...`` to mint
       a verification token. Only the SHA-256 hash of the token is
       stored (``verification_token_hash``); the plaintext is handed
       to the subject through a verified channel.
    2. The subject calls ``POST /api/v1/privacy/erasure`` with the
       token. The server hashes it, looks up the matching pending
       row, transitions it to ``scheduled``, and sets
       ``scheduled_for = requested_at + 30 days``. An ``--immediate``
       flag (admin-only) collapses the 30-day buffer.

    Statuses: ``pending_subject_request``, ``scheduled``, ``completed``,
    ``refused``, ``failed``. The 30-day buffer matches the GDPR
    Art. 12(3) "without undue delay and in any event within one month"
    window while leaving room for the daily retention worker to pick
    up the request and execute it idempotently.

    Audit hold: requests that target classes flagged ``on_expiry: keep``
    in ``data/governed/retention_policy.yaml`` are refused with
    ``refusal_reason='ERASURE_REFUSED_AUDIT_HOLD'``. The endpoint never
    confirms erasure to the requestor before the deletion job actually
    completes -- ``completed_at`` is set by the worker, not the
    request handler.
    """

    __tablename__ = "fund_erasure_requests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(32), default="founder", index=True)
    status: Mapped[str] = mapped_column(
        String(32), default="pending_subject_request", index=True
    )
    verification_token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    issued_by: Mapped[str] = mapped_column(String(128), default="")
    requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_by: Mapped[str] = mapped_column(String(128), default="")
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    immediate: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    classes_json: Mapped[str] = mapped_column(Text, default="[]")
    refusal_reason: Mapped[str] = mapped_column(String(64), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class COIDeclaration(Base):
    """Standing partner declaration of a conflict-creating relationship (prompt 59).

    A row is the *partner's* assertion that they have a relationship
    with ``party_id_ref`` (a CRM-side founder or company id) of kind
    ``relationship`` between ``period_start`` and ``period_end``.
    Only declarations whose validity window covers ``checked_at`` are
    considered live by :func:`check_coi`; expired declarations remain
    in the table for audit but never gate.

    Statuses: ``active``, ``revoked``. ``revoked`` is operator-set
    when a declaration was made in error -- it is treated identically
    to "expired" by the gate but distinct in the audit trail.
    """

    __tablename__ = "fund_coi_declarations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    partner_id: Mapped[str] = mapped_column(String(128), index=True)
    party_kind: Mapped[str] = mapped_column(
        String(16), default="company", server_default="company"
    )
    party_id_ref: Mapped[str] = mapped_column(String(128), index=True)
    relationship: Mapped[str] = mapped_column(String(32), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_uri: Mapped[str] = mapped_column(Text, default="", server_default="")
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class COICheck(Base):
    """Result of a single ``check_coi(application, partner)`` evaluation (prompt 59).

    Append-only by convention: every meeting-proposal attempt and
    every decision-finalization runs the gate and writes a fresh
    row, so the table is the canonical disclosure trail.

    Status values:

    * ``clear`` -- no declaration matched.
    * ``conflicted`` -- at least one ``employed`` / ``family`` /
      ``invested`` / ``board`` / ``founder`` declaration matched.
      The application MUST NOT be auto-routed back to this partner;
      the caller routes to a different partner or to manual review.
    * ``requires_disclosure`` -- a softer relationship (``advisor``
      etc.) matched. The action is permitted only if a
      :class:`COIOverride` row with a justification is attached, or
      if the caller explicitly attaches a disclosure URI before
      proceeding.
    """

    __tablename__ = "fund_coi_checks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    partner_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    evidence_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]"
    )
    disclosure_uri: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    override_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class COIOverride(Base):
    """Admin-issued override of a ``conflicted`` / ``requires_disclosure``
    COI gate (prompt 59).

    Every override carries a ``justification`` of at least 50
    characters and is keyed to a specific ``(application_id,
    partner_id)`` pair so a single override does not silently apply
    to other applications routed to the same partner. Auto-clearing
    is forbidden -- the row exists only because an admin explicitly
    wrote it.
    """

    __tablename__ = "fund_coi_overrides"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    partner_id: Mapped[str] = mapped_column(String(128), index=True)
    justification: Mapped[str] = mapped_column(Text)
    overridden_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class CostEvent(Base):
    """Per-application cost-telemetry record for a paid external call (prompt 62).

    One row per recorded paid event: an LLM token bundle, an STT
    minute, an embeddings batch, a Twilio voice minute, a Stripe fee.
    ``application_id`` is nullable so cross-cutting infra cost
    (background polling, baseline jobs) can still be attributed
    without a specific application.

    The ``unit_cost_usd`` and ``total_usd`` columns are derived
    server-side from ``data/governed/cost_pricing.yaml`` keyed by
    ``sku``; the units count is computed from the *observed* input/
    output (never trusted from a client). Recording is idempotent on
    ``idempotency_key`` (unique index): the second call with the same
    key returns the existing row instead of double-billing.
    """

    __tablename__ = "fund_cost_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str | None] = mapped_column(
        ForeignKey("fund_applications.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), index=True)
    sku: Mapped[str] = mapped_column(String(128), index=True)
    units: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    unit: Mapped[str] = mapped_column(String(32), default="", server_default="")
    unit_cost_usd: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0"
    )
    total_usd: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0"
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class CostAlertState(Base):
    """Cooldown ledger for budget-exceeded alerts (prompt 62).

    One row per ``(scope, scope_key)`` pair where ``scope`` is one of
    ``application`` (``scope_key = application_id``) or ``daily``
    (``scope_key = YYYY-MM-DD`` UTC date). ``last_alert_at`` is the
    timestamp of the most recent ``cost_budget_exceeded`` event we
    emitted for that pair; the alerts service refuses to fire again
    inside the configured cooldown window so a runaway counter does
    not produce a flood of pages.
    """

    __tablename__ = "fund_cost_alert_state"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32))
    scope_key: Mapped[str] = mapped_column(String(128))
    last_alert_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    last_total_usd: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0"
    )


class CapTableIssuance(Base):
    """Local ledger row for a cap-table issuance (prompt 68).

    One row per ``(application, idempotency_key)`` records the
    issuance of a security to a founder after the upstream
    investment workflow has reached its terminal "operator caused
    it" state -- i.e. the SAFE / term-sheet has a ``signed``
    :class:`SignatureRequest` AND the corresponding
    :class:`InvestmentInstruction` has reached ``sent``. Until both
    conditions hold the row MUST NOT be created -- the system records
    issuances; it does not unilaterally issue securities.

    Lifecycle:

        pending --(provider sync)--> recorded --(reconcile)--> reconciled
                                              \\
                                               +--> failed (terminal; retry creates a new key)

    Storage discipline (prompt 68 prohibition): ``provider_issuance_id``
    is the upstream's opaque identifier and is informational only --
    a reconcile that finds a divergence between the local row and
    the provider response is FLAGGED, never auto-healed by trusting
    the provider's value.
    """

    __tablename__ = "fund_cap_table_issuances"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("fund_applications.id"), index=True
    )
    instrument_type: Mapped[str] = mapped_column(String(64))
    amount_usd: Mapped[int] = mapped_column(Integer)
    valuation_cap_usd: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    discount: Mapped[float] = mapped_column(
        Float, default=0.0, server_default="0"
    )
    board_consent_uri: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(32))
    provider_issuance_id: Mapped[str] = mapped_column(
        String(255), default="", index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PolicyParameterProposal(Base):
    """Reserve-allocation optimizer proposal (prompt 70).

    Each row is a single operator-submitted *proposal* of a new
    decision-policy parameter set produced by
    :func:`reserve_optimizer.optimize`. The optimizer never auto-promotes
    -- the row begins life in ``proposed`` status and an admin must
    explicitly transition it to ``approved`` before any downstream
    consumer (e.g. an operator runbook) is allowed to promote the
    parameters into the running decision policy.

    The ``parameters_json`` blob is the canonical
    ``OptimizerResult.to_canonical_dict()`` payload (proposed +
    current + delta + cost-of-error model + audit). It is stored as a
    single JSON string rather than a normalized table so the audit
    trail is exactly the bytes the operator reviewed.

    Statuses (see ``policy_parameter_proposals.VALID_PROPOSAL_STATUSES``):

    * ``proposed`` -- newly inserted, awaiting review.
    * ``under_review`` -- the partner committee has picked it up.
    * ``approved`` -- admin explicitly approved; the
      ``policy_parameter_approved.v1`` event has been emitted.
    * ``rejected`` -- admin explicitly rejected; ``rationale`` is
      appended with the reject reason.
    """

    __tablename__ = "fund_policy_parameter_proposals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    proposed_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    parameters_json: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, default="", server_default="")
    backtest_report_uri: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(32), default="proposed", server_default="proposed", index=True
    )
    approved_by: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class PolicyParameterApproval(Base):
    """Append-only audit row for a proposal-approval transition (prompt 70).

    A row is written for every ``approve`` *or* ``reject`` transition
    applied to a :class:`PolicyParameterProposal` so the lifecycle is
    fully auditable independent of the proposal row's mutable
    ``status`` / ``approved_by`` fields. Approval and rejection share
    the same table because both require admin authorization and both
    are terminal transitions.
    """

    __tablename__ = "fund_policy_parameter_approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("fund_policy_parameter_proposals.id"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16), index=True)
    decided_by: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


# Re-export PIIClearAuditLog so ``Base.metadata.create_all`` sees it
# whenever :mod:`models` is imported (which is the standard entry
# point in tests / Alembic env). Defined in
# :mod:`server.fund.services.pii_clear_audit` so the audit / decrypt
# logic stays co-located with its only sanctioned caller.
from coherence_engine.server.fund.services.pii_clear_audit import (  # noqa: E402,F401
    PIIClearAuditLog,
)
