"""Policy parameter proposal lifecycle tests (prompt 70).

Covers:

* Persistence: ``create`` writes a row in ``proposed`` status with the
  canonical optimizer payload.
* Rate-limit: a second proposal for an overlapping domain within
  ``MIN_PROPOSAL_INTERVAL_DAYS`` raises ``ProposalRateLimited``.
* RBAC: ``approve`` without the ``admin`` role raises
  ``ProposalForbidden`` (the CLI / router maps this onto a 403).
* Approval emits a ``policy_parameter_approved.v1`` outbox row.
* Rejection is admin-only and is terminal.
* The diff renderer surfaces ``per_domain``, ``liquidity_reserve``,
  ``pipeline_volume_cap``, and ``backtest`` keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import reserve_optimizer as ro
from coherence_engine.server.fund.services import policy_parameter_proposals as pp


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "policy"


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _build_optimizer_payload() -> dict:
    payload = json.loads((FIXTURES / "snapshot.json").read_text(encoding="utf-8"))
    study = json.loads((FIXTURES / "study.json").read_text(encoding="utf-8"))
    inputs = ro.OptimizerInputs.from_payload(
        portfolio_snapshot=payload["portfolio_snapshot"],
        validation_study=study,
        historical_rows=payload["historical_rows"],
        projected_pipeline_volume=payload["projected_pipeline_volume"],
        false_pass_budget_usd=payload["false_pass_budget_usd"],
        seed=0,
    )
    return ro.optimize(inputs).to_canonical_dict()


def test_create_persists_proposal_in_proposed_status():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        row = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Synthetic test rationale spanning enough characters.",
        )
        db.commit()
        assert row.status == pp.PROPOSAL_STATUS_PROPOSED
        assert row.proposed_by == "cli"
        assert row.id
        # The blob round-trips into JSON
        stored = json.loads(row.parameters_json)
        assert "proposed" in stored and "current" in stored and "delta" in stored
    finally:
        db.close()


def test_create_rate_limits_overlapping_domain_within_30_days():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="First proposal rationale spans enough characters.",
        )
        db.commit()
        with pytest.raises(pp.ProposalRateLimited):
            svc.create(
                proposed_by="cli",
                parameters=parameters,
                rationale="Second proposal within window must be rejected.",
            )
    finally:
        db.close()


def test_create_allows_proposal_after_rate_limit_window():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        old_ts = datetime.now(tz=timezone.utc) - timedelta(
            days=pp.MIN_PROPOSAL_INTERVAL_DAYS + 1
        )
        first = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Old proposal rationale, well outside the window.",
            now=old_ts,
        )
        # Backdate the persisted row so the rate-limit query finds it
        # outside the window
        first.created_at = old_ts
        db.commit()
        second = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Fresh proposal rationale outside rate-limit window.",
        )
        db.commit()
        assert second.id != first.id
    finally:
        db.close()


def test_create_rejects_short_rationale():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        with pytest.raises(pp.ProposalError) as exc:
            svc.create(
                proposed_by="cli",
                parameters=parameters,
                rationale="too short",
            )
        assert exc.value.code == "RATIONALE_TOO_SHORT"
    finally:
        db.close()


def test_approve_without_admin_role_raises_forbidden():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        row = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Synthetic test rationale spanning enough characters.",
        )
        db.commit()
        with pytest.raises(pp.ProposalForbidden):
            svc.approve(row.id, principal={"id": "viewer", "role": "viewer"})
        # row state must not change
        db.refresh(row)
        assert row.status == pp.PROPOSAL_STATUS_PROPOSED
    finally:
        db.close()


def test_approve_with_admin_emits_event_and_marks_approved():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        row = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Synthetic test rationale spanning enough characters.",
        )
        db.commit()
        approved = svc.approve(
            row.id, principal={"id": "ops-admin", "role": "admin"}
        )
        db.commit()
        assert approved.status == pp.PROPOSAL_STATUS_APPROVED
        assert approved.approved_by == "ops-admin"
        assert approved.approved_at is not None
        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type == pp.PROPOSAL_APPROVED_EVENT_TYPE)
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["proposal_id"] == row.id
        assert payload["approved_by"] == "ops-admin"
        assert "domains" in payload and len(payload["domains"]) >= 1
    finally:
        db.close()


def test_double_approve_raises_invalid_transition():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        row = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Synthetic test rationale spanning enough characters.",
        )
        db.commit()
        svc.approve(row.id, principal={"id": "ops-admin", "role": "admin"})
        db.commit()
        with pytest.raises(pp.ProposalInvalidTransition):
            svc.approve(row.id, principal={"id": "ops-admin", "role": "admin"})
    finally:
        db.close()


def test_reject_requires_admin_and_is_terminal():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        row = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Synthetic test rationale spanning enough characters.",
        )
        db.commit()
        with pytest.raises(pp.ProposalForbidden):
            svc.reject(row.id, principal={"id": "viewer", "role": "viewer"})
        rejected = svc.reject(
            row.id,
            principal={"id": "ops-admin", "role": "admin"},
            reason="risk team disagrees with proposed CS0_d for governance",
        )
        db.commit()
        assert rejected.status == pp.PROPOSAL_STATUS_REJECTED
        assert "rejected:" in rejected.rationale
        with pytest.raises(pp.ProposalInvalidTransition):
            svc.approve(row.id, principal={"id": "ops-admin", "role": "admin"})
    finally:
        db.close()


def test_render_review_returns_diff_with_required_keys():
    parameters = _build_optimizer_payload()
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        row = svc.create(
            proposed_by="cli",
            parameters=parameters,
            rationale="Synthetic test rationale spanning enough characters.",
        )
        db.commit()
        review = svc.render_review(row.id)
        assert review["id"] == row.id
        assert review["status"] == pp.PROPOSAL_STATUS_PROPOSED
        diff = review["diff"]
        for key in ("per_domain", "liquidity_reserve", "pipeline_volume_cap", "backtest"):
            assert key in diff, f"diff missing {key!r}"
    finally:
        db.close()


def test_get_unknown_proposal_raises_not_found():
    db = SessionLocal()
    try:
        svc = pp.PolicyParameterProposalService(db)
        with pytest.raises(pp.ProposalNotFound):
            svc.get("does-not-exist")
    finally:
        db.close()
