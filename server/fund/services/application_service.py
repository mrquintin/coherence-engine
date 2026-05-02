"""Application orchestration service."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from coherence_engine.core.types import Transcript
from coherence_engine.server.fund import models
from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository
from coherence_engine.server.fund.services.decision_artifact import (
    ARTIFACT_KIND,
    build_decision_artifact,
    persist_decision_artifact,
    validate_artifact,
)
from coherence_engine.server.fund.services.decision_policy import (
    DECISION_POLICY_VERSION,
    DecisionPolicyService,
)
from coherence_engine.server.fund.services.event_publisher import (
    SCORING_MODE_ENFORCE,
    SCORING_MODE_SHADOW,
    EventPublisher,
)
from coherence_engine.server.fund.services.scoring import ScoringService
from coherence_engine.server.fund.services.transcript_quality import (
    TranscriptQualityReport,
    evaluate_transcript,
)

_LOG_ARTIFACT = logging.getLogger(__name__)

SHADOW_ARTIFACT_KIND = "shadow_decision_artifact"


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class ApplicationService:
    """Coordinates repository writes, scoring, policy, and event publishing."""

    def __init__(
        self,
        repository: ApplicationRepository,
        events: EventPublisher,
        scoring: Optional[ScoringService] = None,
        policy: Optional[DecisionPolicyService] = None,
        notification_backend: Optional[Any] = None,
        notification_dry_run_dir: Optional[Any] = None,
    ):
        self.repository = repository
        self.events = events
        self.scoring = scoring or ScoringService()
        self.policy = policy or DecisionPolicyService()
        # Prompt 14: notification service. Defaults to a ``DryRunBackend``
        # rooted at ``notification_dry_run_dir`` (or a tmp subdir) so that
        # enforce-mode decisions always dispatch through a real backend —
        # shadow-mode decisions skip dispatch entirely (prompt 12
        # prohibition on founder/partner side effects). Tests that want
        # to assert on the rendered body can inject their own backend.
        self._notification_backend = notification_backend
        self._notification_dry_run_dir = notification_dry_run_dir

    @staticmethod
    def _build_artifact_app_state(
        *,
        app: Any,
        session_id: str,
        score: Dict[str, Any],
        decision_payload: Dict[str, Any],
        canonical_decision: str,
    ) -> Dict[str, Any]:
        """Assemble the deterministic ``app_state`` consumed by build_decision_artifact.

        Inputs in the digest are restricted to fields authored on the
        application + scored payload. ``occurred_at`` uses the application's
        ``updated_at`` ISO timestamp (never wall-clock) so two runs against
        the same persisted state produce byte-identical artifacts.
        """
        ci = score.get("coherence_superiority_ci95") or {}
        per_layer = dict(score.get("layer_scores") or {})
        ci_lower = float(ci.get("lower", 0.0))
        ci_upper = float(ci.get("upper", 0.0))
        composite = float(score.get("absolute_coherence", 0.0))

        normative_profile = {"rights": 0.0, "utilitarian": 0.0, "deontic": 0.0}
        try:
            from coherence_engine.core.types import Proposition
            from coherence_engine.domain.normative import compute_normative_profile

            argument = score.get("argument") or {}
            propositions = []
            for p in (argument.get("propositions") or []):
                propositions.append(
                    Proposition(
                        id=str(p.get("id", "")),
                        text=str(p.get("text", "")),
                        prop_type=str(p.get("type", "premise")),
                        importance=float(p.get("importance", 0.5)),
                    )
                )
            np_obj = compute_normative_profile(propositions)
            normative_profile = {
                "rights": float(np_obj.rights),
                "utilitarian": float(np_obj.utilitarian),
                "deontic": float(np_obj.deontic),
            }
        except Exception:  # pragma: no cover - normative is best-effort
            pass

        domain_primary = str(getattr(app, "domain_primary", "market_economics"))
        domain = {
            "weights": [{"domain": domain_primary, "weight": 1.0}],
            "normative_profile": normative_profile,
            "schema_version": "domain-mix-v1",
            "notes": [],
        }

        reason_codes = sorted({
            str(g.get("reason_code", ""))
            for g in (decision_payload.get("failed_gates") or [])
            if g.get("reason_code")
        })

        # Inputs digest: only authored content + scored numeric features.
        # Excludes any wall-clock timestamps deliberately.
        inputs = {
            "application": {
                "id": str(getattr(app, "id", "")),
                "founder_id": str(getattr(app, "founder_id", "")),
                "one_liner": str(getattr(app, "one_liner", "") or ""),
                "use_of_funds_summary": str(getattr(app, "use_of_funds_summary", "") or ""),
                "requested_check_usd": int(getattr(app, "requested_check_usd", 0) or 0),
                "domain_primary": domain_primary,
                "compliance_status": str(getattr(app, "compliance_status", "clear")),
                "transcript_text": str(getattr(app, "transcript_text", "") or ""),
                "transcript_uri": str(getattr(app, "transcript_uri", "") or ""),
            },
            "session_id": str(session_id),
            "scoring": {
                "coherence_result_id": str(score.get("coherence_result_id", "")),
                "absolute_coherence": composite,
                "baseline_coherence": float(score.get("baseline_coherence", 0.0)),
                "coherence_superiority": float(score.get("coherence_superiority", 0.0)),
                "coherence_superiority_ci95": {"lower": ci_lower, "upper": ci_upper},
                "layer_scores": per_layer,
                "anti_gaming_score": float(score.get("anti_gaming_score", 0.0)),
                "transcript_quality_score": float(score.get("transcript_quality_score", 0.0)),
                "n_contradictions": int(score.get("n_contradictions", 0)),
                "model_versions": dict(score.get("model_versions") or {}),
            },
            "decision": {
                "verdict": canonical_decision,
                "threshold_required": float(decision_payload.get("threshold_required", 0.0)),
                "coherence_observed": float(decision_payload.get("coherence_observed", 0.0)),
                "margin": float(decision_payload.get("margin", 0.0)),
                "policy_version": str(decision_payload.get("policy_version", "")),
                "parameter_set_id": str(decision_payload.get("parameter_set_id", "")),
                "reason_codes": reason_codes,
            },
        }

        occurred_at_dt = getattr(app, "updated_at", None) or getattr(app, "created_at", None)
        if isinstance(occurred_at_dt, datetime):
            occurred_at = occurred_at_dt.isoformat()
        else:
            occurred_at = str(occurred_at_dt or "")

        return {
            "application_id": str(app.id),
            "session_id": str(session_id),
            "occurred_at": occurred_at,
            "inputs": inputs,
            "scoring": {
                "composite": composite,
                "per_layer": per_layer,
                "uncertainty": {"lower": ci_lower, "upper": ci_upper},
                "scoring_version": "scoring-v1.0.0",
            },
            "domain": domain,
            "ontology_graph_id": "",
            "ontology_graph_digest": "",
            "decision": {
                "verdict": canonical_decision,
                "cs_superiority": float(score.get("coherence_superiority", 0.0)),
                "cs_required": float(decision_payload.get("threshold_required", 0.0)),
                "reason_codes": reason_codes,
                "decision_policy_version": DECISION_POLICY_VERSION,
            },
        }

    @staticmethod
    def make_trace_id(request_id: str) -> str:
        return request_id.replace("req_", "trc_", 1) if request_id.startswith("req_") else f"trc_{request_id}"

    def create_application(self, payload: Dict[str, Any]) -> Dict[str, str]:
        domain = self.scoring.detect_domain(payload["startup"]["one_liner"])
        return self.repository.create_application(payload, domain)

    def create_interview_session(self, application_id: str, channel: str, locale: str) -> Dict[str, str]:
        return self.repository.create_interview_session(application_id, channel, locale)

    def trigger_scoring(
        self,
        application_id: str,
        mode: str,
        dry_run: bool,
        trace_id: str,
        idempotency_key: str,
        transcript_text: str | None = None,
        transcript_uri: str | None = None,
        transcript: Transcript | None = None,
    ) -> Dict[str, Any]:
        app = self.repository.get_application(application_id)
        if not app:
            raise ValueError("application_not_found")

        if transcript_text is not None or transcript_uri is not None:
            self.repository.update_application_transcript(application_id, transcript_text, transcript_uri)

        if transcript is not None:
            report = evaluate_transcript(transcript)
            if not report.passed:
                self._handle_transcript_rejection(
                    application_id=application_id,
                    transcript=transcript,
                    report=report,
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                    scoring_mode=str(getattr(app, "scoring_mode", SCORING_MODE_ENFORCE)),
                )
                return {
                    "status": "rejected",
                    "reason": "transcript_quality_gate",
                    "reason_codes": list(report.reason_codes),
                    "score": report.score,
                }

        job = self.repository.create_scoring_job_with_trace(
            application_id=application_id,
            mode=mode,
            dry_run=dry_run,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
        )
        return {"job_id": job["job_id"], "status": "queued"}

    def _handle_transcript_rejection(
        self,
        *,
        application_id: str,
        transcript: Transcript,
        report: TranscriptQualityReport,
        trace_id: str,
        idempotency_key: str,
        scoring_mode: str = SCORING_MODE_ENFORCE,
    ) -> None:
        self.events.publish(
            event_type="TranscriptRejected",
            producer="transcript-quality-gate",
            trace_id=trace_id,
            idempotency_key=f"{idempotency_key}:TranscriptRejected",
            payload={
                "application_id": application_id,
                "session_id": transcript.session_id,
                "language": transcript.language,
                "asr_model": transcript.asr_model,
                "score": report.score,
                "reason_codes": list(report.reason_codes),
                "metrics": report.metrics,
            },
        )
        # In shadow mode the pipeline still runs, but founder-facing
        # notification side effects are suppressed. The upstream
        # TranscriptRejected event is emitted for telemetry parity.
        if scoring_mode == SCORING_MODE_SHADOW:
            return
        self.events.publish(
            event_type="FounderNotified",
            producer="transcript-quality-gate",
            trace_id=trace_id,
            idempotency_key=f"{idempotency_key}:FounderNotified",
            payload={
                "application_id": application_id,
                "channel": "dry_run",
                "template_id": "transcript_quality_rejection_v1",
                "notification_status": "suppressed",
                "failure_reason": ",".join(report.reason_codes),
            },
        )

    def process_next_scoring_job(
        self,
        worker_id: str = "scoring-worker",
        lease_seconds: int = 120,
        retry_base_seconds: int = 5,
    ) -> Optional[Dict[str, Any]]:
        job = self.repository.claim_next_scoring_job(worker_id=worker_id, lease_seconds=lease_seconds)
        if not job:
            return None
        app = self.repository.get_application(job.application_id)
        if not app:
            self.repository.mark_scoring_job_failed(job.id, "application_not_found")
            return {"job_id": job.id, "status": "failed", "reason": "application_not_found"}

        # Idempotency guard: if decision already exists, mark completed and skip re-emission.
        existing_decision = self.repository.get_decision(app.id)
        if existing_decision:
            self.repository.mark_scoring_job_completed(job.id)
            return {"job_id": job.id, "status": "completed", "reason": "decision_already_exists"}

        try:
            score = self.scoring.score_application(application=app)

            propositions_json = json.dumps(score["argument"]["propositions"])
            relations_json = json.dumps(score["argument"]["relations"])
            artifact = self.repository.create_argument_artifact(
                application_id=app.id,
                scoring_job_id=job.id,
                propositions_json=propositions_json,
                relations_json=relations_json,
            )

            # Publish interview completion event when transcript is available.
            transcript_uri = app.transcript_uri or f"db://applications/{app.id}/transcript_text"
            session_id = (
                app.interview_sessions[-1].id
                if app.interview_sessions
                else f"ivw_{app.id[-12:]}"
            )
            self.events.publish(
                event_type="InterviewCompleted",
                producer="conversation-orchestrator",
                trace_id=job.trace_id or f"trc_{job.id}",
                idempotency_key=f"{job.idempotency_key}:InterviewCompleted",
                payload={
                    "application_id": app.id,
                    # Canonical v1 fields (see server/fund/schemas/events/interview_completed.v1.json).
                    "session_id": session_id,
                    "transcript_ref": transcript_uri,
                    "duration_s": 600,
                    "asr_confidence_avg": score["transcript_quality_score"],
                    # Legacy/auxiliary fields retained for downstream consumers.
                    "founder_id": app.founder_id,
                    "interview_id": session_id,
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
                    "transcript_uri": transcript_uri,
                    "audio_uri": f"db://applications/{app.id}/audio_placeholder",
                    "transcript_quality_score": score["transcript_quality_score"],
                    "consent_status": "granted",
                    "language": "en-US",
                },
            )

            self.events.publish(
                event_type="ArgumentCompiled",
                producer="argument-compiler",
                trace_id=job.trace_id or f"trc_{job.id}",
                idempotency_key=f"{job.idempotency_key}:ArgumentCompiled",
                payload={
                    "application_id": app.id,
                    # Canonical v1 field (see server/fund/schemas/events/argument_compiled.v1.json).
                    "argument_graph_ref": artifact["artifact_id"],
                    "n_propositions": len(score["argument"]["propositions"]),
                    "n_relations": len(score["argument"]["relations"]),
                    # Legacy/auxiliary fields retained for downstream consumers.
                    "argument_id": artifact["artifact_id"],
                    "propositions_uri": artifact["propositions_uri"],
                    "relations_uri": artifact["relations_uri"],
                    "compiler_version": "argument-compiler-v0.2.0",
                    "quality_flags": [],
                },
            )
            self.events.publish(
                event_type="CoherenceScored",
                producer="coherence-engine-service",
                trace_id=job.trace_id or f"trc_{job.id}",
                idempotency_key=f"{job.idempotency_key}:CoherenceScored",
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

            portfolio_state = self.repository.get_portfolio_state_snapshot(
                application_id=app.id,
                founder_id=app.founder_id,
                domain_primary=app.domain_primary,
            )
            decision_payload = self.policy.evaluate(
                application={
                    "domain_primary": app.domain_primary,
                    "requested_check_usd": app.requested_check_usd,
                    "compliance_status": app.compliance_status,
                },
                score_record=score,
                portfolio_state=portfolio_state,
            )
            upserted = self.repository.upsert_decision(app.id, decision_payload)
            decision_id = upserted["decision_id"]
            canonical_decision = {"fail": "reject"}.get(
                decision_payload["decision"], decision_payload["decision"]
            )

            # Build + persist a reproducible decision_artifact.v1 bundle.
            # The artifact pins inputs, per-layer scores, decision outcome, and
            # versions; ``occurred_at`` is taken from the application's
            # authored timestamps (never wall-clock) so the digest is stable.
            artifact_app_state = self._build_artifact_app_state(
                app=app,
                session_id=session_id,
                score=score,
                decision_payload=decision_payload,
                canonical_decision=canonical_decision,
            )
            decision_artifact = build_decision_artifact(artifact_app_state)

            # Read the per-application scoring mode. ``enforce`` runs the
            # existing behavior unchanged; ``shadow`` still builds and
            # persists the artifact but uses a distinct ``kind`` so it is
            # clearly separable from production artifacts, and tags the
            # ``DecisionIssued`` event with ``mode="shadow"`` so the
            # outbox dispatcher can route or filter downstream.
            scoring_mode = str(getattr(app, "scoring_mode", SCORING_MODE_ENFORCE)) or SCORING_MODE_ENFORCE
            is_shadow = scoring_mode == SCORING_MODE_SHADOW
            artifact_kind = SHADOW_ARTIFACT_KIND if is_shadow else ARTIFACT_KIND

            try:
                if is_shadow:
                    self._persist_shadow_decision_artifact(
                        app_id=app.id,
                        artifact_dict=decision_artifact,
                        scoring_job_id=job.id,
                    )
                else:
                    persist_decision_artifact(
                        self.repository.db,
                        app_id=app.id,
                        artifact_dict=decision_artifact,
                        scoring_job_id=job.id,
                    )
            except Exception:  # pragma: no cover - persistence is best-effort
                _LOG_ARTIFACT.exception(
                    "decision_artifact_persist_failed application_id=%s kind=%s",
                    app.id,
                    artifact_kind,
                )

            self.events.publish(
                event_type="DecisionIssued",
                producer="decision-policy-engine",
                trace_id=job.trace_id or f"trc_{job.id}",
                idempotency_key=f"{job.idempotency_key}:DecisionIssued",
                payload={
                    "application_id": app.id,
                    # Canonical v1 fields (see server/fund/schemas/events/decision_issued.v1.json).
                    # Internal policy vocabulary 'fail' is published as 'reject'.
                    "decision": canonical_decision,
                    "cs_superiority": score["coherence_superiority"],
                    "cs_required": decision_payload["threshold_required"],
                    "decision_policy_version": decision_payload["policy_version"],
                    "scoring_version": "scoring-v1.0.0",
                    # Optional `mode` (prompt 12): ``enforce`` = production
                    # behavior, ``shadow`` = side-effect-suppressed replay.
                    "mode": scoring_mode,
                    # Legacy/auxiliary fields retained for downstream consumers.
                    "decision_id": decision_id,
                    "threshold_required": decision_payload["threshold_required"],
                    "coherence_observed": decision_payload["coherence_observed"],
                    "margin": decision_payload["margin"],
                    "failed_gates": decision_payload["failed_gates"],
                    "policy_version": decision_payload["policy_version"],
                    "parameter_set_id": decision_payload["parameter_set_id"],
                },
            )
            # Prompt 14 — dispatch the founder notification for
            # enforce-mode decisions. Shadow-mode runs skip dispatch
            # entirely (prompt 12 prohibition). Failures are logged
            # but do not fail the scoring job: the DecisionIssued
            # event and the decision row are the authoritative outputs;
            # notification delivery is a downstream best-effort side
            # effect whose retries live in the NotificationLog ledger.
            if not is_shadow:
                try:
                    self._dispatch_enforce_notification(
                        application_id=app.id,
                        canonical_decision=canonical_decision,
                    )
                except Exception:  # pragma: no cover - best-effort
                    _LOG_ARTIFACT.exception(
                        "founder_notification_dispatch_failed application_id=%s decision=%s",
                        app.id,
                        canonical_decision,
                    )
                # Prompt 54 -- on a ``pass`` decision, emit a
                # ``scheduling_requested`` event so the scheduler
                # can offer partner-meeting slots to the founder.
                # Outbox-only: the actual Cal.com / Google Calendar
                # call is deferred to the scheduler service path so
                # a backend outage never blocks the decision write.
                if canonical_decision == "pass":
                    try:
                        from coherence_engine.server.fund.services.scheduler import (
                            emit_scheduling_event,
                        )

                        emit_scheduling_event(
                            self.events,
                            application_id=app.id,
                            partner_email=self._resolve_partner_email(app),
                            trace_id=job.trace_id or f"trc_{job.id}",
                            idempotency_key=f"{job.idempotency_key}:SchedulingRequested",
                        )
                    except Exception:  # pragma: no cover - best-effort
                        _LOG_ARTIFACT.exception(
                            "scheduling_requested_emit_failed application_id=%s",
                            app.id,
                        )

            self.repository.mark_scoring_job_completed(job.id)
            return {
                "job_id": job.id,
                "status": "completed",
                "decision_id": decision_id,
                "scoring_mode": scoring_mode,
            }
        except Exception as exc:
            outcome = self.repository.fail_or_retry_scoring_job(
                job_id=job.id,
                error_message=str(exc),
                base_retry_seconds=retry_base_seconds,
            )
            return {"job_id": job.id, "status": outcome, "reason": str(exc)}

    def run_application_workflow(
        self,
        application_id: str,
        *,
        resume: bool = False,
        force: bool = False,
        trace_id: str = "",
        idempotency_prefix: str = "",
        scoring_job_id: str = "",
    ):
        """Delegate to the workflow orchestrator (prompt 15).

        Runs the ``intake -> transcript_quality -> compile -> ontology ->
        domain_mix -> score -> decide -> artifact -> notify`` pipeline
        with per-stage checkpoint rows so retries resume at the failing
        stage. Wraps the same underlying services (scoring, decision
        policy, decision artifact, notification) used by
        :meth:`process_next_scoring_job`; does NOT reorder events.

        Returns the persisted :class:`WorkflowRun` row.
        """
        from coherence_engine.server.fund.services.workflow import run_workflow

        return run_workflow(
            self.repository.db,
            application_id,
            resume=resume,
            force=force,
            trace_id=trace_id,
            idempotency_prefix=idempotency_prefix,
            scoring_job_id=scoring_job_id,
            notification_backend=self._notification_backend,
            notification_dry_run_dir=self._notification_dry_run_dir,
        )

    def _resolve_notification_backend(self):
        """Return the configured backend, defaulting to a ``DryRunBackend``.

        The default backend writes under
        ``self._notification_dry_run_dir`` when provided, falling back
        to ``./var/notifications`` (relative to the current working
        directory). This preserves the prompt 14 prohibition on real
        emails from CI while still producing inspectable envelopes.
        """
        if self._notification_backend is not None:
            return self._notification_backend
        from pathlib import Path as _Path

        from coherence_engine.server.fund.services.notification_backends import (
            DryRunBackend,
        )

        root = self._notification_dry_run_dir or _Path.cwd() / "var" / "notifications"
        return DryRunBackend(_Path(root))

    def _resolve_partner_email(self, application: Any) -> str:
        """Return the partner email used for scheduler proposals.

        The dedicated partner-routing surface (round-robin across
        active partners) lives in a separate workstream; until it
        lands the env override ``SCHEDULER_DEFAULT_PARTNER_EMAIL``
        is the single fallback so the ``scheduling_requested``
        event always carries a non-empty address.
        """
        import os as _os

        return _os.getenv(
            "SCHEDULER_DEFAULT_PARTNER_EMAIL",
            "partners@coherence.fund",
        )

    def _dispatch_enforce_notification(
        self,
        *,
        application_id: str,
        canonical_decision: str,
    ) -> None:
        """Dispatch the founder notification for a freshly-written enforce decision.

        Called only on ``scoring_mode == "enforce"`` paths (shadow mode
        skips dispatch per prompt 12). The dispatch is idempotent on
        ``(application_id, template_id)`` so a replay / retry will not
        re-send.
        """
        from coherence_engine.server.fund.services.notifications import (
            NotificationError,
            dispatch as _notify_dispatch,
        )

        backend = self._resolve_notification_backend()
        try:
            _notify_dispatch(
                session=self.repository.db,
                application_id=application_id,
                verdict=canonical_decision,
                backend=backend,
            )
        except NotificationError as exc:
            _LOG_ARTIFACT.warning(
                "founder_notification_dispatch_failed application_id=%s decision=%s reason=%s",
                application_id,
                canonical_decision,
                exc,
            )

    def get_decision(self, application_id: str) -> Dict[str, Any]:
        decision = self.repository.get_decision(application_id)
        if not decision:
            return {
                "application_id": application_id,
                "decision_id": "",
                "decision": "pending",
                "policy_version": "decision-policy-v1.0.0",
                "threshold_required": 0.0,
                "coherence_observed": 0.0,
                "margin": 0.0,
                "failed_gates": [],
                "updated_at": _utc_now_iso(),
            }
        return {
            "application_id": decision.application_id,
            "decision_id": decision.id,
            "decision": decision.decision,
            "policy_version": decision.policy_version,
            "threshold_required": decision.threshold_required,
            "coherence_observed": decision.coherence_observed,
            "margin": decision.margin,
            "failed_gates": json.loads(decision.failed_gates_json),
            "updated_at": decision.updated_at.isoformat(),
        }

    def create_escalation_packet(
        self,
        application_id: str,
        partner_email: str,
        include_calendar_link: bool,
    ) -> Dict[str, Any]:
        app = self.repository.get_application(application_id)
        if app is not None and str(getattr(app, "scoring_mode", SCORING_MODE_ENFORCE)) == SCORING_MODE_SHADOW:
            # Partner escalation is a side effect — forbidden in shadow mode.
            raise RuntimeError("escalation_forbidden_in_shadow_mode")
        decision = self.repository.get_decision(application_id)
        if not decision:
            raise RuntimeError("decision_not_available")
        if decision.decision != "pass":
            raise RuntimeError(f"decision_not_pass:{decision.decision}")
        return self.repository.create_escalation_packet(
            application_id=application_id,
            decision_id=decision.id,
            partner_email=partner_email,
            include_calendar_link=include_calendar_link,
        )

    def _persist_shadow_decision_artifact(
        self,
        *,
        app_id: str,
        artifact_dict: Dict[str, Any],
        scoring_job_id: Optional[str],
    ) -> models.ArgumentArtifact:
        """Persist a decision_artifact with ``kind='shadow_decision_artifact'``.

        Mirrors :func:`persist_decision_artifact` (same schema validation and
        canonical JSON serialization) but writes a distinct ``kind`` so
        operators and downstream consumers can cleanly separate shadow
        replays from production artifacts.
        """
        validate_artifact(artifact_dict)
        payload_json = json.dumps(artifact_dict, sort_keys=True, separators=(",", ":"))
        rec = models.ArgumentArtifact(
            id=str(artifact_dict["artifact_id"]),
            application_id=str(app_id),
            scoring_job_id=str(scoring_job_id or ""),
            propositions_json="[]",
            relations_json="[]",
            kind=SHADOW_ARTIFACT_KIND,
            payload_json=payload_json,
        )
        self.repository.db.add(rec)
        self.repository.db.flush()
        return rec

    def maybe_sync_cap_table(
        self,
        application_id: str,
        *,
        backend: Optional[Any] = None,
        instrument_type: str = "safe_post_money",
        valuation_cap_usd: int = 0,
        discount: float = 0.0,
        board_consent_uri: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Attempt a cap-table issuance sync after sign + fund (prompt 68).

        Idempotent and conditional: returns ``None`` when the upstream
        gates (signed SAFE + sent investment instruction) are not yet
        both satisfied, otherwise returns a small descriptor of the
        recorded :class:`CapTableIssuance`. Repeat calls collapse onto
        the same row via the deterministic idempotency key.

        Designed to be called from both the e-signature webhook (after
        a request transitions to ``signed``) and the capital webhook
        (after an instruction transitions to ``sent``); whichever
        side fires second is the one that performs the sync.
        """
        from coherence_engine.server.fund.services.cap_table import (
            CapTableService,
            compute_idempotency_key,
            preconditions_satisfied,
        )

        signature, instruction = preconditions_satisfied(
            self.repository.db, application_id=application_id
        )
        if signature is None or instruction is None:
            return None

        if backend is None:
            from coherence_engine.server.fund.services.cap_table_backends import (
                CapTableBackendConfigError,
                backend_for_provider,
            )
            import os as _os

            provider = _os.getenv("CAP_TABLE_PROVIDER", "carta")
            try:
                backend = backend_for_provider(provider)
            except CapTableBackendConfigError as exc:
                _LOG_ARTIFACT.warning(
                    "cap_table_sync_skipped application_id=%s reason=config:%s",
                    application_id,
                    exc,
                )
                return None

        key = compute_idempotency_key(
            application_id, instrument_type, salt=instruction.id
        )
        service = CapTableService(self.repository.db)
        row = service.record_issuance(
            backend=backend,
            application_id=application_id,
            instrument_type=instrument_type,
            amount_usd=int(instruction.amount_usd),
            valuation_cap_usd=int(valuation_cap_usd or 0),
            discount=float(discount or 0.0),
            board_consent_uri=board_consent_uri,
            idempotency_key=key,
            verify_preconditions=False,
        )
        return {
            "issuance_id": row.id,
            "application_id": application_id,
            "provider": row.provider,
            "provider_issuance_id": row.provider_issuance_id,
            "status": row.status,
        }

    def set_scoring_mode(
        self,
        application_id: str,
        *,
        new_mode: str,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Transition an application between ``enforce`` and ``shadow`` modes.

        Refuses the ``enforce -> shadow`` transition after a decision has
        already been issued unless ``force`` is ``True`` (prompt 12
        guardrail: we must not quietly retroactively redirect a
        production decision's side effects).
        """
        new_mode = (new_mode or "").strip().lower()
        if new_mode not in {SCORING_MODE_ENFORCE, SCORING_MODE_SHADOW}:
            raise ValueError(f"invalid_scoring_mode:{new_mode!r}")
        app = self.repository.get_application(application_id)
        if app is None:
            raise ValueError("application_not_found")
        previous = str(getattr(app, "scoring_mode", SCORING_MODE_ENFORCE)) or SCORING_MODE_ENFORCE
        if previous == new_mode:
            return {
                "application_id": application_id,
                "previous_mode": previous,
                "new_mode": new_mode,
                "changed": False,
            }
        if (
            previous == SCORING_MODE_ENFORCE
            and new_mode == SCORING_MODE_SHADOW
            and not force
        ):
            existing_decision = self.repository.get_decision(application_id)
            if existing_decision is not None:
                raise RuntimeError(
                    "enforce_to_shadow_forbidden_after_decision_issued"
                )
        app.scoring_mode = new_mode  # type: ignore[assignment]
        self.repository.db.add(app)
        self.repository.db.flush()
        return {
            "application_id": application_id,
            "previous_mode": previous,
            "new_mode": new_mode,
            "changed": True,
        }
