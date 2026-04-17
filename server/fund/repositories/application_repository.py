"""Repository layer for fund workflow persistence."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ApplicationRepository:
    """Encapsulates DB operations for application lifecycle."""

    def __init__(self, db: Session):
        self.db = db

    def get_idempotency_response(self, endpoint: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        stmt: Select[tuple[models.IdempotencyRecord]] = select(models.IdempotencyRecord).where(
            models.IdempotencyRecord.endpoint == endpoint,
            models.IdempotencyRecord.idempotency_key == idempotency_key,
        )
        rec = self.db.execute(stmt).scalar_one_or_none()
        if not rec:
            return None
        return json.loads(rec.response_json)

    def save_idempotency_response(self, endpoint: str, idempotency_key: str, response_payload: Dict[str, Any]) -> None:
        rec = models.IdempotencyRecord(
            id=_new_id("idem"),
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            response_json=json.dumps(response_payload),
        )
        self.db.add(rec)
        self.db.flush()

    def create_application(self, payload: Dict[str, Any], domain_primary: str) -> Dict[str, Any]:
        founder_id = _new_id("fnd")
        application_id = _new_id("app")

        founder = models.Founder(
            id=founder_id,
            full_name=payload["founder"]["full_name"],
            email=payload["founder"]["email"],
            company_name=payload["founder"]["company_name"],
            country=payload["founder"]["country"],
        )
        app = models.Application(
            id=application_id,
            founder_id=founder_id,
            one_liner=payload["startup"]["one_liner"],
            requested_check_usd=payload["startup"]["requested_check_usd"],
            use_of_funds_summary=payload["startup"]["use_of_funds_summary"],
            preferred_channel=payload["startup"]["preferred_channel"],
            transcript_text=payload["startup"].get("transcript_text"),
            transcript_uri=payload["startup"].get("transcript_uri"),
            domain_primary=domain_primary,
            compliance_status="clear",
            status="intake_created",
        )
        self.db.add(founder)
        self.db.add(app)
        self.db.flush()
        return {"application_id": application_id, "founder_id": founder_id}

    def get_application(self, application_id: str) -> Optional[models.Application]:
        stmt = select(models.Application).where(models.Application.id == application_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_portfolio_state_snapshot(
        self, application_id: str, founder_id: str, domain_primary: str
    ) -> Dict[str, Any]:
        """Aggregate portfolio snapshot from existing rows plus env-tunable hooks (no schema migration)."""
        raw_capacity = os.environ.get("COHERENCE_NOTIONAL_CAPACITY_USD", "12000000")
        try:
            notional_capacity_usd = float(raw_capacity)
        except (TypeError, ValueError):
            notional_capacity_usd = 12_000_000.0
        if notional_capacity_usd < 1.0:
            notional_capacity_usd = 1.0

        raw_liquidity_fraction = os.environ.get("COHERENCE_LIQUIDITY_RESERVE_FRACTION", "0.05")
        try:
            liquidity_reserve_fraction = float(raw_liquidity_fraction)
        except (TypeError, ValueError):
            liquidity_reserve_fraction = 0.05
        if liquidity_reserve_fraction < 0.0:
            liquidity_reserve_fraction = 0.0
        if liquidity_reserve_fraction > 1.0:
            liquidity_reserve_fraction = 1.0

        regime_raw = (os.environ.get("COHERENCE_PORTFOLIO_REGIME", "neutral") or "neutral").strip().lower()
        drawdown_raw = os.environ.get("COHERENCE_PORTFOLIO_DRAWDOWN_PROXY", "0") or "0"
        try:
            portfolio_drawdown_proxy = float(drawdown_raw)
        except ValueError:
            portfolio_drawdown_proxy = 0.0
        if portfolio_drawdown_proxy < 0.0:
            portfolio_drawdown_proxy = 0.0
        if portfolio_drawdown_proxy > 1.0:
            portfolio_drawdown_proxy = 1.0

        committed_stmt = (
            select(func.coalesce(func.sum(models.Application.requested_check_usd), 0))
            .select_from(models.Application)
            .join(models.Decision, models.Decision.application_id == models.Application.id)
            .where(
                models.Decision.decision == "pass",
                models.Application.id != application_id,
            )
        )
        committed_raw = self.db.execute(committed_stmt).scalar_one()
        committed_pass_usd_excl_current = float(committed_raw or 0)

        founder_stmt = (
            select(
                func.count().label("n"),
                func.coalesce(func.sum(models.Application.requested_check_usd), 0).label("s"),
            )
            .select_from(models.Application)
            .join(models.Decision, models.Decision.application_id == models.Application.id)
            .where(
                models.Decision.decision == "pass",
                models.Application.founder_id == founder_id,
                models.Application.id != application_id,
            )
        )
        frow = self.db.execute(founder_stmt).one()
        same_founder_pass_count_excl_current = int(frow.n or 0)
        same_founder_pass_committed_usd_excl_current = float(frow.s or 0)

        domain_stmt = (
            select(func.count())
            .select_from(models.Application)
            .join(models.Decision, models.Decision.application_id == models.Application.id)
            .where(
                models.Decision.decision == "pass",
                models.Application.domain_primary == domain_primary,
                models.Application.id != application_id,
            )
        )
        domain_pass_count_excl_current = int(self.db.execute(domain_stmt).scalar_one() or 0)

        domain_usd_stmt = (
            select(func.coalesce(func.sum(models.Application.requested_check_usd), 0))
            .select_from(models.Application)
            .join(models.Decision, models.Decision.application_id == models.Application.id)
            .where(
                models.Decision.decision == "pass",
                models.Application.domain_primary == domain_primary,
                models.Application.id != application_id,
            )
        )
        domain_pass_committed_usd_raw = self.db.execute(domain_usd_stmt).scalar_one()
        domain_pass_committed_usd_excl_current = float(domain_pass_committed_usd_raw or 0)

        open_stmt = (
            select(func.count())
            .select_from(models.Application)
            .outerjoin(models.Decision, models.Decision.application_id == models.Application.id)
            .where(
                models.Application.id != application_id,
                models.Decision.id.is_(None),
            )
        )
        open_pipeline_count_excl_current = int(self.db.execute(open_stmt).scalar_one() or 0)

        dry_powder_usd_excl_current = max(0.0, notional_capacity_usd - committed_pass_usd_excl_current)
        liquidity_reserve_floor_usd = notional_capacity_usd * liquidity_reserve_fraction

        return {
            "notional_capacity_usd": notional_capacity_usd,
            "committed_pass_usd_excl_current": committed_pass_usd_excl_current,
            "same_founder_pass_count_excl_current": same_founder_pass_count_excl_current,
            "same_founder_pass_committed_usd_excl_current": same_founder_pass_committed_usd_excl_current,
            "open_pipeline_count_excl_current": open_pipeline_count_excl_current,
            "domain_pass_count_excl_current": domain_pass_count_excl_current,
            "domain_pass_committed_usd_excl_current": domain_pass_committed_usd_excl_current,
            "dry_powder_usd_excl_current": dry_powder_usd_excl_current,
            "liquidity_reserve_floor_usd": liquidity_reserve_floor_usd,
            "portfolio_regime_code": regime_raw,
            "portfolio_drawdown_proxy": portfolio_drawdown_proxy,
        }

    def create_interview_session(self, application_id: str, channel: str, locale: str) -> Dict[str, Any]:
        interview_id = _new_id("ivw")
        rec = models.InterviewSession(
            id=interview_id,
            application_id=application_id,
            channel=channel,
            locale=locale,
            status="active",
        )
        app = self.get_application(application_id)
        if app:
            app.status = "interview_in_progress"
            app.updated_at = _utc_now()
        self.db.add(rec)
        self.db.flush()
        return {"interview_id": interview_id}

    def create_scoring_job(self, application_id: str, mode: str, dry_run: bool) -> Dict[str, Any]:
        return self.create_scoring_job_with_trace(
            application_id=application_id,
            mode=mode,
            dry_run=dry_run,
            trace_id="",
            idempotency_key="",
        )

    def create_scoring_job_with_trace(
        self,
        application_id: str,
        mode: str,
        dry_run: bool,
        trace_id: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        job_id = _new_id("job")
        rec = models.ScoringJob(
            id=job_id,
            application_id=application_id,
            mode=mode,
            dry_run=dry_run,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            status="queued",
        )
        app = self.get_application(application_id)
        if app:
            app.status = "scoring_queued"
            app.updated_at = _utc_now()
        self.db.add(rec)
        self.db.flush()
        return {"job_id": job_id}

    def claim_next_scoring_job(self, worker_id: str = "scoring-worker", lease_seconds: int = 120) -> Optional[models.ScoringJob]:
        now = _utc_now()
        lease_until = now + timedelta(seconds=max(1, lease_seconds))
        eligible = and_(
            or_(
                models.ScoringJob.status == "queued",
                and_(
                    models.ScoringJob.status == "processing",
                    models.ScoringJob.lease_expires_at.is_not(None),
                    models.ScoringJob.lease_expires_at < now,
                ),
            ),
            or_(
                models.ScoringJob.next_attempt_at.is_(None),
                models.ScoringJob.next_attempt_at <= now,
            ),
            models.ScoringJob.attempts < models.ScoringJob.max_attempts,
        )

        for _ in range(5):
            candidate_id = self.db.execute(
                select(models.ScoringJob.id)
                .where(eligible)
                .order_by(models.ScoringJob.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if not candidate_id:
                return None

            updated = self.db.execute(
                update(models.ScoringJob)
                .where(models.ScoringJob.id == candidate_id)
                .where(eligible)
                .values(
                    status="processing",
                    started_at=now,
                    completed_at=None,
                    lease_expires_at=lease_until,
                    locked_by=worker_id[:128],
                    attempts=models.ScoringJob.attempts + 1,
                    next_attempt_at=None,
                )
            )
            if updated.rowcount != 1:
                continue

            rec = self.db.execute(
                select(models.ScoringJob).where(models.ScoringJob.id == candidate_id)
            ).scalar_one_or_none()
            if not rec:
                return None
            app = self.get_application(rec.application_id)
            if app:
                app.status = "scoring_in_progress"
                app.updated_at = _utc_now()
            self.db.flush()
            return rec
        return None

    def mark_scoring_job_completed(self, job_id: str) -> None:
        stmt = select(models.ScoringJob).where(models.ScoringJob.id == job_id)
        rec = self.db.execute(stmt).scalar_one_or_none()
        if not rec:
            return
        rec.status = "completed"
        rec.completed_at = _utc_now()
        rec.error_message = ""
        rec.lease_expires_at = None
        rec.locked_by = ""
        rec.next_attempt_at = None
        app = self.get_application(rec.application_id)
        if app and app.status.startswith("scoring_"):
            app.updated_at = _utc_now()
        self.db.flush()

    def mark_scoring_job_failed(self, job_id: str, error_message: str) -> None:
        stmt = select(models.ScoringJob).where(models.ScoringJob.id == job_id)
        rec = self.db.execute(stmt).scalar_one_or_none()
        if not rec:
            return
        rec.status = "failed"
        rec.completed_at = _utc_now()
        rec.error_message = error_message[:4000]
        rec.lease_expires_at = None
        rec.locked_by = ""
        rec.next_attempt_at = None
        app = self.get_application(rec.application_id)
        if app:
            app.status = "scoring_failed"
            app.updated_at = _utc_now()
        self.db.flush()

    def fail_or_retry_scoring_job(self, job_id: str, error_message: str, base_retry_seconds: int = 5) -> str:
        stmt = select(models.ScoringJob).where(models.ScoringJob.id == job_id)
        rec = self.db.execute(stmt).scalar_one_or_none()
        if not rec:
            return "failed"

        safe_base = max(1, int(base_retry_seconds))
        if int(rec.attempts or 0) >= int(rec.max_attempts or 1):
            self.mark_scoring_job_failed(job_id, error_message)
            return "failed"

        retry_delay = safe_base * (2 ** max(0, int(rec.attempts or 1) - 1))
        rec.status = "queued"
        rec.error_message = error_message[:4000]
        rec.next_attempt_at = _utc_now() + timedelta(seconds=retry_delay)
        rec.lease_expires_at = None
        rec.locked_by = ""
        rec.completed_at = None
        app = self.get_application(rec.application_id)
        if app:
            app.status = "scoring_retry_pending"
            app.updated_at = _utc_now()
        self.db.flush()
        return "retry_scheduled"

    def list_dead_letter_scoring_jobs(self, limit: int = 100) -> List[models.ScoringJob]:
        stmt = (
            select(models.ScoringJob)
            .where(models.ScoringJob.status == "failed")
            .order_by(models.ScoringJob.completed_at.desc(), models.ScoringJob.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def replay_scoring_jobs(self, job_ids: List[str] | None = None, limit: int = 100, reset_attempts: bool = False) -> int:
        stmt = select(models.ScoringJob).where(models.ScoringJob.status == "failed")
        if job_ids:
            stmt = stmt.where(models.ScoringJob.id.in_(job_ids))
        else:
            stmt = stmt.limit(limit)
        rows = list(self.db.execute(stmt).scalars().all())
        for rec in rows:
            rec.status = "queued"
            rec.error_message = ""
            rec.next_attempt_at = _utc_now()
            rec.lease_expires_at = None
            rec.locked_by = ""
            rec.completed_at = None
            if reset_attempts:
                rec.attempts = 0
            app = self.get_application(rec.application_id)
            if app:
                app.status = "scoring_queued"
                app.updated_at = _utc_now()
        self.db.flush()
        return len(rows)

    def update_application_transcript(
        self,
        application_id: str,
        transcript_text: Optional[str],
        transcript_uri: Optional[str],
    ) -> None:
        app = self.get_application(application_id)
        if not app:
            return
        if transcript_text is not None:
            app.transcript_text = transcript_text
        if transcript_uri is not None:
            app.transcript_uri = transcript_uri
        app.updated_at = _utc_now()
        self.db.flush()

    def create_argument_artifact(
        self,
        application_id: str,
        scoring_job_id: str,
        propositions_json: str,
        relations_json: str,
    ) -> Dict[str, str]:
        artifact_id = _new_id("arg")
        rec = models.ArgumentArtifact(
            id=artifact_id,
            application_id=application_id,
            scoring_job_id=scoring_job_id,
            propositions_json=propositions_json,
            relations_json=relations_json,
        )
        self.db.add(rec)
        self.db.flush()

        propositions_uri = f"db://fund_argument_artifacts/{artifact_id}/propositions"
        relations_uri = f"db://fund_argument_artifacts/{artifact_id}/relations"
        app = self.get_application(application_id)
        if app:
            app.argument_propositions_uri = propositions_uri
            app.argument_relations_uri = relations_uri
            app.updated_at = _utc_now()
        self.db.flush()
        return {
            "artifact_id": artifact_id,
            "propositions_uri": propositions_uri,
            "relations_uri": relations_uri,
        }

    def list_interview_sessions(self, application_id: str) -> List[models.InterviewSession]:
        stmt = (
            select(models.InterviewSession)
            .where(models.InterviewSession.application_id == application_id)
            .order_by(models.InterviewSession.created_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def upsert_decision(self, application_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        stmt = select(models.Decision).where(models.Decision.application_id == application_id)
        existing = self.db.execute(stmt).scalar_one_or_none()
        decision_id = existing.id if existing else _new_id("dec")
        failed_json = json.dumps(payload["failed_gates"])
        if existing:
            existing.decision = payload["decision"]
            existing.policy_version = payload["policy_version"]
            existing.parameter_set_id = payload["parameter_set_id"]
            existing.threshold_required = payload["threshold_required"]
            existing.coherence_observed = payload["coherence_observed"]
            existing.margin = payload["margin"]
            existing.failed_gates_json = failed_json
            existing.updated_at = _utc_now()
        else:
            existing = models.Decision(
                id=decision_id,
                application_id=application_id,
                decision=payload["decision"],
                policy_version=payload["policy_version"],
                parameter_set_id=payload["parameter_set_id"],
                threshold_required=payload["threshold_required"],
                coherence_observed=payload["coherence_observed"],
                margin=payload["margin"],
                failed_gates_json=failed_json,
                updated_at=_utc_now(),
            )
            self.db.add(existing)
        app = self.get_application(application_id)
        if app:
            app.status = f"decision_{payload['decision']}"
            app.updated_at = _utc_now()
        self.db.flush()
        return {"decision_id": decision_id}

    def get_decision(self, application_id: str) -> Optional[models.Decision]:
        stmt = select(models.Decision).where(models.Decision.application_id == application_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def create_escalation_packet(
        self,
        application_id: str,
        decision_id: str,
        partner_email: str,
        include_calendar_link: bool,
    ) -> Dict[str, Any]:
        packet_id = _new_id("pkt")
        packet_uri = f"s3://fund-escalations/{packet_id}.md"
        rec = models.EscalationPacket(
            id=packet_id,
            application_id=application_id,
            decision_id=decision_id,
            partner_email=partner_email,
            packet_uri=packet_uri,
            status="sent",
        )
        self.db.add(rec)
        app = self.get_application(application_id)
        if app:
            app.status = "escalated"
            app.updated_at = _utc_now()
        self.db.flush()
        return {
            "packet_id": packet_id,
            "packet_uri": packet_uri,
            "status": "sent",
            "include_calendar_link": include_calendar_link,
        }

