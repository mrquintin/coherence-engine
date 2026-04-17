"""Workflow orchestrator skeleton (prompt 15).

A **thin, in-process, idempotent** orchestrator that composes the fund
application pipeline into nine named stages and persists a checkpoint
row per stage so a failed run can be resumed at the first non-succeeded
step. The orchestrator is **not** a replacement for
:meth:`coherence_engine.server.fund.services.application_service.ApplicationService.process_next_scoring_job`
— it wraps the same underlying services (scoring, decision policy,
decision artifact, notification) used by that worker and emits the same
events in the same order.

Stages
------

The pipeline is a fixed, ordered list (see :data:`STEPS`)::

    intake
        verify application exists and is pipelineable

    transcript_quality
        verify the transcript is not blocked / empty

    compile
        run the scorer, persist ArgumentArtifact, publish
        ``InterviewCompleted`` + ``ArgumentCompiled`` events

    ontology
        record the ontology + normative slice of the score

    domain_mix
        record the domain / layer-score slice of the score

    score
        publish the ``CoherenceScored`` event

    decide
        load portfolio state, run the decision policy, upsert Decision

    artifact
        build + persist the decision_artifact.v1 bundle and
        publish the ``DecisionIssued`` event

    notify
        dispatch the founder notification (skipped in shadow mode)

Checkpoint model
----------------

Each stage persists a :class:`~coherence_engine.server.fund.models.WorkflowStep`
row keyed on ``(workflow_run_id, name)``. The ``input_digest`` column
is the SHA-256 of the canonical JSON of the stage's declared inputs;
on resume the orchestrator recomputes this digest for every already-
succeeded step and refuses to resume if any upstream input has drifted
(``force=True`` bypasses the check but leaves an audit trail).

Idempotency
-----------

* Stage side effects are idempotent at the service layer:
  ``ScoringService`` is deterministic, ``upsert_decision`` is an
  UPSERT, ``persist_decision_artifact`` is keyed by digest, the
  notification service is keyed on ``(application_id, template_id)``.
* Stages that publish events use stable, application-scoped
  idempotency keys so two workflow runs for the same application
  produce outbox rows whose dedup keys match their single-run
  counterparts.
* A succeeded stage is **never** re-executed by a resume.

Prohibitions (prompt 15)
------------------------

* No Celery / Temporal / Prefect / Airflow dependency — in-process,
  synchronous execution only.
* No reordering of events — this orchestrator invokes the same
  service methods as ``process_next_scoring_job`` and publishes
  ``InterviewCompleted`` → ``ArgumentCompiled`` → ``CoherenceScored``
  → ``DecisionIssued`` in that order.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.repositories.application_repository import (
    ApplicationRepository,
)
from coherence_engine.server.fund.services.decision_artifact import (
    ARTIFACT_KIND,
    build_decision_artifact,
    persist_decision_artifact,
)
from coherence_engine.server.fund.services.decision_policy import (
    DecisionPolicyService,
)
from coherence_engine.server.fund.services.event_publisher import (
    SCORING_MODE_ENFORCE,
    SCORING_MODE_SHADOW,
    EventPublisher,
)
from coherence_engine.server.fund.services.ops_telemetry import record_stage
from coherence_engine.server.fund.services.scoring import ScoringService


__all__ = [
    "STEPS",
    "STEP_NAMES",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "SHADOW_ARTIFACT_KIND",
    "WorkflowError",
    "WorkflowResumeRefused",
    "WorkflowContext",
    "run_workflow",
    "compute_digest",
    "canonicalize",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage stopwatch (prompt 18)
#
# A tiny monotonic-clock helper used to measure per-stage wall-clock
# duration and emit one ``record_stage`` event per stage. Kept local
# (rather than in ``ops_telemetry``) so the orchestrator stays in
# charge of its own tracing surface and so the import graph for
# ``ops_telemetry`` remains free of workflow-specific concepts.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def stopwatch() -> Iterator[Callable[[], float]]:
    """Monotonic wall-clock stopwatch.

    Usage::

        with stopwatch() as elapsed:
            fn()
        record_stage(name, elapsed(), "success")

    ``elapsed()`` may be called before the ``with`` block exits (to
    get a running total) and after it exits (to get the final
    duration). Always returns a non-negative float.
    """
    start = time.monotonic()
    frozen: List[Optional[float]] = [None]

    def _elapsed() -> float:
        if frozen[0] is not None:
            return frozen[0]
        delta = time.monotonic() - start
        return delta if delta > 0 else 0.0

    try:
        yield _elapsed
    finally:
        delta = time.monotonic() - start
        frozen[0] = delta if delta > 0 else 0.0


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Mirrors application_service.SHADOW_ARTIFACT_KIND; duplicated here to
# avoid a circular import (application_service imports workflow).
SHADOW_ARTIFACT_KIND = "shadow_decision_artifact"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkflowError(Exception):
    """Raised on validation / orchestration failures (missing app, unknown
    step, etc.). Transport / stage failures are re-raised from the stage
    body unchanged so the caller sees the original exception while the
    checkpoint row records ``status='failed'`` + an operator-readable
    ``error`` string.
    """


class WorkflowResumeRefused(WorkflowError):
    """Raised when resume detects an ``input_digest`` mismatch on an
    already-succeeded stage and ``force`` was not set."""


# ---------------------------------------------------------------------------
# Canonicalization + digest helpers
# ---------------------------------------------------------------------------


def canonicalize(value: Any) -> Any:
    """Return a JSON-canonicalizable form of ``value``.

    Sorts mapping keys recursively and coerces non-JSON-native scalars
    to strings. Used exclusively for digest computation — NOT for
    reconstructing stage inputs.
    """
    if isinstance(value, dict):
        return {str(k): canonicalize(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def compute_digest(value: Any) -> str:
    """SHA-256 of the canonical JSON representation of ``value``."""
    payload = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------


@dataclass
class WorkflowContext:
    """Mutable per-run state threaded through the stage functions.

    Built up incrementally as stages execute. Stages that need a value
    call the corresponding ``_ensure_*`` helper which lazily populates
    the field from DB / service calls; this lets a resumed run rebuild
    state for any previously-succeeded stage without re-executing its
    side effects.
    """

    session: Session
    application_id: str
    trace_id: str = ""
    idempotency_prefix: str = ""
    notification_backend: Optional[Any] = None
    notification_dry_run_dir: Optional[Path] = None
    # Lazily populated fields:
    app: Optional[Any] = None
    founder: Optional[Any] = None
    scoring_service: Optional[ScoringService] = None
    policy_service: Optional[DecisionPolicyService] = None
    events: Optional[EventPublisher] = None
    score: Optional[Dict[str, Any]] = None
    argument_artifact: Optional[Dict[str, Any]] = None
    session_id: str = ""
    transcript_uri: str = ""
    decision_payload: Optional[Dict[str, Any]] = None
    decision_id: str = ""
    canonical_decision: str = ""
    decision_artifact: Optional[Dict[str, Any]] = None
    scoring_mode: str = SCORING_MODE_ENFORCE
    extras: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage-local helpers (lazy derivations)
# ---------------------------------------------------------------------------


def _ensure_services(ctx: WorkflowContext) -> None:
    if ctx.scoring_service is None:
        ctx.scoring_service = ScoringService()
    if ctx.policy_service is None:
        ctx.policy_service = DecisionPolicyService()
    if ctx.events is None:
        ctx.events = EventPublisher(ctx.session, strict_events=False)


def _ensure_app(ctx: WorkflowContext) -> Any:
    if ctx.app is not None:
        return ctx.app
    app = (
        ctx.session.query(models.Application)
        .filter(models.Application.id == ctx.application_id)
        .one_or_none()
    )
    if app is None:
        raise WorkflowError(f"application_not_found:{ctx.application_id}")
    ctx.app = app
    if app.founder_id:
        ctx.founder = (
            ctx.session.query(models.Founder)
            .filter(models.Founder.id == app.founder_id)
            .one_or_none()
        )
    ctx.scoring_mode = (
        str(getattr(app, "scoring_mode", SCORING_MODE_ENFORCE))
        or SCORING_MODE_ENFORCE
    )
    ctx.transcript_uri = (
        app.transcript_uri or f"db://applications/{app.id}/transcript_text"
    )
    ctx.session_id = (
        app.interview_sessions[-1].id
        if getattr(app, "interview_sessions", None)
        else f"ivw_{app.id[-12:]}"
    )
    return app


def _ensure_score(ctx: WorkflowContext) -> Dict[str, Any]:
    """Run the scorer once per workflow run; re-runs are deterministic."""
    if ctx.score is not None:
        return ctx.score
    _ensure_services(ctx)
    _ensure_app(ctx)
    ctx.score = ctx.scoring_service.score_application(application=ctx.app)
    return ctx.score


def _ensure_decision_payload(ctx: WorkflowContext) -> Dict[str, Any]:
    if ctx.decision_payload is not None:
        return ctx.decision_payload
    _ensure_services(ctx)
    _ensure_app(ctx)
    score = _ensure_score(ctx)
    repo = ApplicationRepository(ctx.session)
    portfolio_state = repo.get_portfolio_state_snapshot(
        application_id=ctx.app.id,
        founder_id=ctx.app.founder_id,
        domain_primary=ctx.app.domain_primary,
    )
    payload = ctx.policy_service.evaluate(
        application={
            "domain_primary": ctx.app.domain_primary,
            "requested_check_usd": ctx.app.requested_check_usd,
            "compliance_status": ctx.app.compliance_status,
        },
        score_record=score,
        portfolio_state=portfolio_state,
    )
    ctx.decision_payload = payload
    ctx.canonical_decision = {"fail": "reject"}.get(
        payload["decision"], payload["decision"]
    )
    return payload


# ---------------------------------------------------------------------------
# Stage input specifications (for input_digest computation)
# ---------------------------------------------------------------------------


def _stage_inputs(name: str, ctx: WorkflowContext) -> Dict[str, Any]:
    """Return a deterministic dict describing the inputs a stage will consume.

    Captures only stable, hash-friendly fields — NOT wall-clock times or
    auto-incrementing ids — so the computed ``input_digest`` is a pure
    function of upstream persisted state. A mismatch on resume signals
    that some upstream input was mutated since the original run.
    """
    app = _ensure_app(ctx)
    transcript_text = getattr(app, "transcript_text", "") or ""
    base = {
        "application_id": str(app.id),
        "founder_id": str(app.founder_id or ""),
        "domain_primary": str(app.domain_primary or ""),
        "compliance_status": str(app.compliance_status or ""),
        "requested_check_usd": int(app.requested_check_usd or 0),
        "scoring_mode": ctx.scoring_mode,
        "transcript_len": len(transcript_text),
        "transcript_sha256": hashlib.sha256(
            transcript_text.encode("utf-8")
        ).hexdigest(),
        "transcript_uri": str(app.transcript_uri or ""),
    }
    if name == "intake":
        return {"application_id": base["application_id"]}
    if name == "transcript_quality":
        return {
            "application_id": base["application_id"],
            "compliance_status": base["compliance_status"],
            "transcript_len": base["transcript_len"],
            "transcript_sha256": base["transcript_sha256"],
        }
    if name == "compile":
        return {
            "application_id": base["application_id"],
            "transcript_sha256": base["transcript_sha256"],
            "transcript_len": base["transcript_len"],
            "domain_primary": base["domain_primary"],
        }
    if name in ("ontology", "domain_mix", "score"):
        return {
            "application_id": base["application_id"],
            "transcript_sha256": base["transcript_sha256"],
            "domain_primary": base["domain_primary"],
        }
    if name == "decide":
        return {
            "application_id": base["application_id"],
            "transcript_sha256": base["transcript_sha256"],
            "domain_primary": base["domain_primary"],
            "compliance_status": base["compliance_status"],
            "requested_check_usd": base["requested_check_usd"],
        }
    if name == "artifact":
        return {
            "application_id": base["application_id"],
            "transcript_sha256": base["transcript_sha256"],
            "domain_primary": base["domain_primary"],
            "compliance_status": base["compliance_status"],
            "scoring_mode": base["scoring_mode"],
        }
    if name == "notify":
        return {
            "application_id": base["application_id"],
            "scoring_mode": base["scoring_mode"],
        }
    raise WorkflowError(f"unknown_stage:{name}")


# ---------------------------------------------------------------------------
# Stage bodies
#
# Each ``_stage_<name>`` returns a JSON-serializable dict that gets
# hashed into ``output_digest``. Side effects (DB writes, event
# publishes) happen inside the body and are idempotent at the service
# layer (see module docstring).
# ---------------------------------------------------------------------------


def _stage_intake(ctx: WorkflowContext) -> Dict[str, Any]:
    app = _ensure_app(ctx)
    return {
        "application_id": app.id,
        "founder_id": app.founder_id,
        "scoring_mode": ctx.scoring_mode,
    }


def _stage_transcript_quality(ctx: WorkflowContext) -> Dict[str, Any]:
    app = _ensure_app(ctx)
    text = getattr(app, "transcript_text", "") or ""
    uri = getattr(app, "transcript_uri", "") or ""
    if str(app.compliance_status or "").lower() == "blocked":
        raise WorkflowError(f"transcript_quality_blocked:{app.id}")
    if not text and not uri:
        raise WorkflowError(f"transcript_quality_empty:{app.id}")
    return {
        "compliance_status": str(app.compliance_status or ""),
        "transcript_len": len(text),
        "has_uri": bool(uri),
        "passed": True,
    }


def _stage_compile(ctx: WorkflowContext) -> Dict[str, Any]:
    _ensure_services(ctx)
    app = _ensure_app(ctx)
    score = _ensure_score(ctx)
    repo = ApplicationRepository(ctx.session)

    propositions_json = json.dumps(score["argument"]["propositions"])
    relations_json = json.dumps(score["argument"]["relations"])
    artifact = repo.create_argument_artifact(
        application_id=app.id,
        scoring_job_id=ctx.extras.get("scoring_job_id", ""),
        propositions_json=propositions_json,
        relations_json=relations_json,
    )
    ctx.argument_artifact = artifact

    idem = ctx.idempotency_prefix or f"workflow:{app.id}"
    ctx.events.publish(
        event_type="InterviewCompleted",
        producer="conversation-orchestrator",
        trace_id=ctx.trace_id or f"trc_{app.id}",
        idempotency_key=f"{idem}:InterviewCompleted",
        payload={
            "application_id": app.id,
            "session_id": ctx.session_id,
            "transcript_ref": ctx.transcript_uri,
            "duration_s": 600,
            "asr_confidence_avg": score["transcript_quality_score"],
            "founder_id": app.founder_id,
            "interview_id": ctx.session_id,
            "channel": app.preferred_channel,
            "duration_seconds": 600,
            "sections_covered": [
                "problem",
                "solution",
                "evidence",
                "market",
                "moat",
                "execution",
                "risk",
                "funding_request",
            ],
            "transcript_uri": ctx.transcript_uri,
            "audio_uri": f"db://applications/{app.id}/audio_placeholder",
            "transcript_quality_score": score["transcript_quality_score"],
            "consent_status": "granted",
            "language": "en-US",
        },
    )
    ctx.events.publish(
        event_type="ArgumentCompiled",
        producer="argument-compiler",
        trace_id=ctx.trace_id or f"trc_{app.id}",
        idempotency_key=f"{idem}:ArgumentCompiled",
        payload={
            "application_id": app.id,
            "argument_graph_ref": artifact["artifact_id"],
            "n_propositions": len(score["argument"]["propositions"]),
            "n_relations": len(score["argument"]["relations"]),
            "argument_id": artifact["artifact_id"],
            "propositions_uri": artifact["propositions_uri"],
            "relations_uri": artifact["relations_uri"],
            "compiler_version": "argument-compiler-v0.2.0",
            "quality_flags": [],
        },
    )
    return {
        "artifact_id": artifact["artifact_id"],
        "n_propositions": len(score["argument"]["propositions"]),
        "n_relations": len(score["argument"]["relations"]),
        "coherence_result_id": score["coherence_result_id"],
    }


def _stage_ontology(ctx: WorkflowContext) -> Dict[str, Any]:
    score = _ensure_score(ctx)
    return {
        "ontology_version": score.get("model_versions", {}).get(
            "ontology_version", ""
        ),
        "n_propositions": len(score["argument"]["propositions"]),
        "n_contradictions": int(score.get("n_contradictions", 0) or 0),
    }


def _stage_domain_mix(ctx: WorkflowContext) -> Dict[str, Any]:
    app = _ensure_app(ctx)
    score = _ensure_score(ctx)
    layer_scores = dict(score.get("layer_scores") or {})
    return {
        "domain_primary": str(app.domain_primary or ""),
        "layer_keys": sorted(layer_scores.keys()),
        "absolute_coherence": float(score.get("absolute_coherence", 0.0) or 0.0),
    }


def _stage_score(ctx: WorkflowContext) -> Dict[str, Any]:
    _ensure_services(ctx)
    app = _ensure_app(ctx)
    score = _ensure_score(ctx)
    idem = ctx.idempotency_prefix or f"workflow:{app.id}"
    ctx.events.publish(
        event_type="CoherenceScored",
        producer="coherence-engine-service",
        trace_id=ctx.trace_id or f"trc_{app.id}",
        idempotency_key=f"{idem}:CoherenceScored",
        payload={
            "application_id": app.id,
            "coherence_result_id": score["coherence_result_id"],
            "absolute_coherence": score["absolute_coherence"],
            "baseline_coherence": score["baseline_coherence"],
            "coherence_superiority": score["coherence_superiority"],
            "coherence_superiority_ci95": score["coherence_superiority_ci95"],
            "layer_scores": score["layer_scores"],
            "anti_gaming_score": score["anti_gaming_score"],
            "model_versions": score["model_versions"],
            "n_propositions": len(score["argument"]["propositions"]),
            "transcript_quality_score": score["transcript_quality_score"],
            "n_contradictions": score.get("n_contradictions", 0),
        },
    )
    return {
        "coherence_superiority": float(score["coherence_superiority"]),
        "coherence_superiority_ci95": dict(score["coherence_superiority_ci95"]),
        "baseline_coherence": float(score["baseline_coherence"]),
        "absolute_coherence": float(score["absolute_coherence"]),
    }


def _stage_decide(ctx: WorkflowContext) -> Dict[str, Any]:
    _ensure_services(ctx)
    app = _ensure_app(ctx)
    payload = _ensure_decision_payload(ctx)
    repo = ApplicationRepository(ctx.session)
    upserted = repo.upsert_decision(app.id, payload)
    ctx.decision_id = upserted["decision_id"]
    return {
        "decision_id": ctx.decision_id,
        "decision": str(payload.get("decision", "")),
        "canonical_decision": ctx.canonical_decision,
        "policy_version": str(payload.get("policy_version", "")),
        "threshold_required": float(payload.get("threshold_required", 0.0) or 0.0),
        "coherence_observed": float(payload.get("coherence_observed", 0.0) or 0.0),
        "margin": float(payload.get("margin", 0.0) or 0.0),
        "failed_gate_count": len(payload.get("failed_gates") or []),
    }


def _stage_artifact(ctx: WorkflowContext) -> Dict[str, Any]:
    _ensure_services(ctx)
    app = _ensure_app(ctx)
    score = _ensure_score(ctx)
    payload = _ensure_decision_payload(ctx)

    # Reuse ApplicationService._build_artifact_app_state (a @staticmethod)
    # so the decision_artifact produced by the workflow is byte-identical
    # to the one produced by ApplicationService.process_next_scoring_job
    # for the same application. Lazy import avoids the circular dependency
    # (application_service imports this module via run_application_workflow).
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )

    artifact_app_state = ApplicationService._build_artifact_app_state(
        app=app,
        session_id=ctx.session_id,
        score=score,
        decision_payload=payload,
        canonical_decision=ctx.canonical_decision,
    )
    decision_artifact = build_decision_artifact(artifact_app_state)
    ctx.decision_artifact = decision_artifact

    is_shadow = ctx.scoring_mode == SCORING_MODE_SHADOW
    artifact_kind = SHADOW_ARTIFACT_KIND if is_shadow else ARTIFACT_KIND

    try:
        if is_shadow:
            from coherence_engine.server.fund.services.decision_artifact import (
                validate_artifact,
            )

            validate_artifact(decision_artifact)
            row = models.ArgumentArtifact(
                id=f"ada_{uuid.uuid4().hex[:16]}",
                application_id=app.id,
                scoring_job_id=ctx.extras.get("scoring_job_id", ""),
                kind=SHADOW_ARTIFACT_KIND,
                propositions_json=json.dumps(decision_artifact),
                relations_json="[]",
            )
            ctx.session.add(row)
            ctx.session.flush()
        else:
            persist_decision_artifact(
                ctx.session,
                app_id=app.id,
                artifact_dict=decision_artifact,
                scoring_job_id=ctx.extras.get("scoring_job_id", ""),
            )
    except Exception:  # pragma: no cover - best-effort persistence
        _LOG.exception(
            "workflow_decision_artifact_persist_failed application_id=%s kind=%s",
            app.id,
            artifact_kind,
        )

    idem = ctx.idempotency_prefix or f"workflow:{app.id}"
    ctx.events.publish(
        event_type="DecisionIssued",
        producer="decision-policy-engine",
        trace_id=ctx.trace_id or f"trc_{app.id}",
        idempotency_key=f"{idem}:DecisionIssued",
        payload={
            "application_id": app.id,
            "decision": ctx.canonical_decision,
            "cs_superiority": score["coherence_superiority"],
            "cs_required": payload["threshold_required"],
            "decision_policy_version": payload["policy_version"],
            "scoring_version": "scoring-v1.0.0",
            "mode": ctx.scoring_mode,
            "decision_id": ctx.decision_id,
            "threshold_required": payload["threshold_required"],
            "coherence_observed": payload["coherence_observed"],
            "margin": payload["margin"],
            "failed_gates": payload["failed_gates"],
            "policy_version": payload["policy_version"],
            "parameter_set_id": payload["parameter_set_id"],
        },
    )

    return {
        "artifact_kind": artifact_kind,
        "scoring_mode": ctx.scoring_mode,
        "decision_id": ctx.decision_id,
        "canonical_decision": ctx.canonical_decision,
    }


def _stage_notify(ctx: WorkflowContext) -> Dict[str, Any]:
    """Dispatch the founder notification (enforce mode only).

    Shadow-mode runs intentionally skip dispatch per prompt 12
    prohibition on founder-facing side effects. The stage still
    records a succeeded checkpoint row (with ``skipped=True`` in the
    output_digest payload) so resume semantics remain consistent.
    """
    _ensure_app(ctx)
    if ctx.scoring_mode == SCORING_MODE_SHADOW:
        return {"skipped": True, "reason": "shadow_mode"}

    from coherence_engine.server.fund.services.notification_backends import (
        DryRunBackend,
    )
    from coherence_engine.server.fund.services.notifications import (
        NotificationError,
        dispatch as _notify_dispatch,
    )

    backend = ctx.notification_backend
    if backend is None:
        root = ctx.notification_dry_run_dir or (
            Path.cwd() / "var" / "notifications"
        )
        backend = DryRunBackend(Path(root))

    try:
        log = _notify_dispatch(
            session=ctx.session,
            application_id=ctx.application_id,
            verdict=ctx.canonical_decision or "manual_review",
            backend=backend,
        )
    except NotificationError as exc:
        _LOG.warning(
            "workflow_notification_dispatch_failed application_id=%s reason=%s",
            ctx.application_id,
            exc,
        )
        return {"dispatched": False, "error": str(exc)[:200]}

    return {
        "dispatched": True,
        "template_id": log.template_id,
        "channel": log.channel,
        "status": log.status,
    }


# ---------------------------------------------------------------------------
# Canonical stage table
# ---------------------------------------------------------------------------

STEPS: List[Tuple[str, Callable[[WorkflowContext], Dict[str, Any]]]] = [
    ("intake", _stage_intake),
    ("transcript_quality", _stage_transcript_quality),
    ("compile", _stage_compile),
    ("ontology", _stage_ontology),
    ("domain_mix", _stage_domain_mix),
    ("score", _stage_score),
    ("decide", _stage_decide),
    ("artifact", _stage_artifact),
    ("notify", _stage_notify),
]

STEP_NAMES: Tuple[str, ...] = tuple(name for name, _ in STEPS)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _load_run_for_resume(
    session: Session, application_id: str
) -> Optional[models.WorkflowRun]:
    """Most recent non-succeeded WorkflowRun for the application, or None.

    Prefers ``failed`` then ``running`` then anything else so the most
    useful resume target wins when the history has multiple rows.
    """
    rows = (
        session.query(models.WorkflowRun)
        .filter(models.WorkflowRun.application_id == application_id)
        .order_by(models.WorkflowRun.created_at.desc())
        .all()
    )
    for row in rows:
        if row.status != STATUS_SUCCEEDED:
            return row
    return rows[0] if rows else None


def _load_steps(
    session: Session, workflow_run_id: str
) -> Dict[str, models.WorkflowStep]:
    rows = (
        session.query(models.WorkflowStep)
        .filter(models.WorkflowStep.workflow_run_id == workflow_run_id)
        .all()
    )
    return {row.name: row for row in rows}


def _new_workflow_run(session: Session, application_id: str) -> models.WorkflowRun:
    run = models.WorkflowRun(
        id=f"wfr_{uuid.uuid4().hex[:16]}",
        application_id=application_id,
        status=STATUS_RUNNING,
        current_step="",
        started_at=_utc_now(),
        finished_at=None,
        error="",
    )
    session.add(run)
    session.flush()
    return run


def run_workflow(
    session: Session,
    application_id: str,
    *,
    resume: bool = False,
    force: bool = False,
    trace_id: str = "",
    idempotency_prefix: str = "",
    scoring_job_id: str = "",
    notification_backend: Optional[Any] = None,
    notification_dry_run_dir: Optional[Path] = None,
) -> models.WorkflowRun:
    """Run (or resume) the fund application workflow for ``application_id``.

    Args:
        session: An open SQLAlchemy session. The caller owns the
            commit; this function flushes but does not commit.
        application_id: The application to process.
        resume: If True, pick up the most recent non-succeeded
            WorkflowRun for this application and continue from the
            first non-succeeded step.
        force: If True, skip the ``input_digest`` drift check when
            resuming. Pairs with ``resume=True``.
        trace_id: Trace identifier threaded through event publishes.
        idempotency_prefix: Prefix used to derive per-event
            ``idempotency_key`` values. Defaults to
            ``f"workflow:{application_id}"`` so two workflow runs
            for the same application produce stable outbox keys.
        scoring_job_id: Optional upstream ScoringJob id (threaded
            into ArgumentArtifact rows for provenance).
        notification_backend: Optional pre-constructed
            :class:`NotificationBackend`; defaults to a DryRunBackend.
        notification_dry_run_dir: Root for the default DryRun backend
            when no backend is injected.

    Returns:
        The persisted :class:`WorkflowRun` row with final status.

    Raises:
        WorkflowError: on validation failures (missing application,
            unknown step, resume state corruption).
        WorkflowResumeRefused: if ``resume=True`` detects a stored
            ``input_digest`` that no longer matches the recomputed
            value and ``force`` was not set.
        Exception: original stage exceptions propagate to the caller
            after the checkpoint row has been marked ``failed``.
    """
    app_id = str(application_id)
    if not app_id:
        raise WorkflowError("application_id_required")

    if resume:
        run = _load_run_for_resume(session, app_id)
        if run is None:
            run = _new_workflow_run(session, app_id)
        else:
            run.status = STATUS_RUNNING
            run.error = ""
            run.finished_at = None
            session.flush()
    else:
        run = _new_workflow_run(session, app_id)

    ctx = WorkflowContext(
        session=session,
        application_id=app_id,
        trace_id=trace_id or f"trc_{app_id}",
        idempotency_prefix=idempotency_prefix or f"workflow:{app_id}",
        notification_backend=notification_backend,
        notification_dry_run_dir=notification_dry_run_dir,
        extras={"scoring_job_id": scoring_job_id},
    )

    try:
        _ensure_app(ctx)
    except WorkflowError as exc:
        run.status = STATUS_FAILED
        run.error = str(exc)[:1000]
        run.finished_at = _utc_now()
        session.flush()
        raise

    existing_steps = _load_steps(session, run.id)

    for name, fn in STEPS:
        step = existing_steps.get(name)
        expected_digest = compute_digest(_stage_inputs(name, ctx))

        if step is not None and step.status == STATUS_SUCCEEDED:
            if not force and step.input_digest and step.input_digest != expected_digest:
                raise WorkflowResumeRefused(
                    f"input_digest_mismatch:{name}:"
                    f"stored={step.input_digest[:12]}...,"
                    f"recomputed={expected_digest[:12]}..."
                )
            # Succeeded and inputs match (or force) -> skip.
            continue

        if step is None:
            step = models.WorkflowStep(
                id=f"wfs_{uuid.uuid4().hex[:16]}",
                workflow_run_id=run.id,
                name=name,
                status=STATUS_RUNNING,
                started_at=_utc_now(),
                finished_at=None,
                input_digest=expected_digest,
                output_digest="",
                error="",
            )
            session.add(step)
            session.flush()
        else:
            step.status = STATUS_RUNNING
            step.started_at = _utc_now()
            step.finished_at = None
            step.input_digest = expected_digest
            step.output_digest = ""
            step.error = ""
            session.flush()

        run.current_step = name
        session.flush()

        # Prompt 18: wrap stage execution with a monotonic stopwatch
        # so every stage (success or failure) emits a single
        # ``record_stage`` telemetry event. The event mirrors the
        # checkpoint row write below — we publish telemetry even on
        # the exception path so the SLO surface never under-counts
        # failure-only runs.
        stage_extra: Dict[str, Any] = {
            "application_id": app_id,
            "workflow_run_id": run.id,
            "workflow_step_id": step.id,
        }
        if ctx.trace_id:
            stage_extra["trace_id"] = ctx.trace_id

        with stopwatch() as elapsed:
            try:
                output = fn(ctx) or {}
            except Exception as exc:
                step.status = STATUS_FAILED
                step.error = f"{type(exc).__name__}:{str(exc)[:500]}"
                step.finished_at = _utc_now()
                run.status = STATUS_FAILED
                run.error = f"{name}:{type(exc).__name__}:{str(exc)[:200]}"
                run.finished_at = _utc_now()
                session.flush()
                record_stage(
                    name,
                    elapsed(),
                    "failure",
                    extra={
                        **stage_extra,
                        "error_type": type(exc).__name__,
                    },
                )
                raise

        step.output_digest = compute_digest(output)
        step.status = STATUS_SUCCEEDED
        step.finished_at = _utc_now()
        session.flush()
        record_stage(name, elapsed(), "success", extra=stage_extra)

    run.status = STATUS_SUCCEEDED
    run.current_step = STEP_NAMES[-1]
    run.error = ""
    run.finished_at = _utc_now()
    session.flush()
    return run
