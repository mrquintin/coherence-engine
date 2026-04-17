"""Tests for portfolio-aware decision policy (R(S, portfolio_state))."""

from __future__ import annotations

import os

import pytest

from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund import models
from coherence_engine.server.fund.repositories.application_repository import ApplicationRepository
from coherence_engine.server.fund.services.decision_policy import DecisionPolicyService


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _passing_score(ci_lower: float = 0.8) -> dict:
    return {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.1,
        "coherence_superiority_ci95": {"lower": ci_lower, "upper": min(ci_lower + 0.1, 0.99)},
    }


def test_evaluate_without_portfolio_state_legacy_envelope():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 50000, "compliance_status": "clear"}
    out = policy.evaluate(app, _passing_score())
    assert set(out.keys()) == {
        "decision",
        "threshold_required",
        "coherence_observed",
        "margin",
        "failed_gates",
        "policy_version",
        "parameter_set_id",
    }
    assert out["decision"] == "pass"
    assert out["policy_version"] == "decision-policy-v1.0.0"
    assert out["parameter_set_id"] == "params_starter_v1"
    assert "portfolio_adjustments" not in out


def test_fund_capacity_exceeded_hard_fail():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 500_000, "compliance_status": "clear"}
    ps = {
        "notional_capacity_usd": 1_000_000.0,
        "committed_pass_usd_excl_current": 600_000.0,
    }
    out = policy.evaluate(app, _passing_score(), portfolio_state=ps)
    assert out["decision"] == "fail"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "PORTFOLIO_FUND_CAPACITY_EXCEEDED" in codes


def test_founder_concentration_manual_review():
    policy = DecisionPolicyService()
    cap = 2_000_000.0
    founder_cap = 0.12 * cap
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": int(founder_cap - 100_000) + 1,
        "compliance_status": "clear",
    }
    ps = {
        "notional_capacity_usd": cap,
        "same_founder_pass_committed_usd_excl_current": 100_000.0,
    }
    out = policy.evaluate(app, _passing_score(), portfolio_state=ps)
    assert out["decision"] == "manual_review"
    assert any(g["reason_code"] == "PORTFOLIO_FOUNDER_CONCENTRATION" for g in out["failed_gates"])


def test_high_utilization_raises_cs_required_changes_outcome():
    policy = DecisionPolicyService()
    requested = 100_000.0
    committed = 10_500_000.0
    ps = {
        "notional_capacity_usd": 12_000_000.0,
        "committed_pass_usd_excl_current": committed,
    }
    app = {"domain_primary": "market_economics", "requested_check_usd": int(requested), "compliance_status": "clear"}
    base = policy.evaluate(app, _passing_score(ci_lower=0.435), portfolio_state=None)
    stressed = policy.evaluate(app, _passing_score(ci_lower=0.435), portfolio_state=ps)
    assert base["decision"] == "pass"
    assert stressed["decision"] == "fail"
    assert stressed["threshold_required"] > base["threshold_required"]
    assert stressed["policy_version"] == "decision-policy-v1.1.0"
    assert "portfolio_adjustments" in stressed
    assert stressed["portfolio_adjustments"]["cs_required_delta"] == pytest.approx(0.01)


def test_repository_portfolio_snapshot_aggregates():
    db = SessionLocal()
    try:
        f1 = models.Founder(
            id="fnd_test1",
            full_name="A",
            email="a@example.com",
            company_name="Co A",
            country="US",
        )
        f2 = models.Founder(
            id="fnd_test2",
            full_name="B",
            email="b@example.com",
            company_name="Co B",
            country="US",
        )
        app_pass = models.Application(
            id="app_pass1",
            founder_id="fnd_test1",
            one_liner="x",
            requested_check_usd=100_000,
            use_of_funds_summary="u",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="decision_pass",
        )
        app_open = models.Application(
            id="app_open1",
            founder_id="fnd_test2",
            one_liner="y",
            requested_check_usd=50_000,
            use_of_funds_summary="u",
            preferred_channel="web_voice",
            domain_primary="governance",
            compliance_status="clear",
            status="intake_created",
        )
        app_current = models.Application(
            id="app_curr1",
            founder_id="fnd_test1",
            one_liner="z",
            requested_check_usd=75_000,
            use_of_funds_summary="u",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="scoring_in_progress",
        )
        dec = models.Decision(
            id="dec_p1",
            application_id="app_pass1",
            decision="pass",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="params_starter_v1",
            threshold_required=0.2,
            coherence_observed=0.3,
            margin=0.1,
            failed_gates_json="[]",
        )
        db.add_all([f1, f2, app_pass, app_open, app_current, dec])
        db.commit()

        repo = ApplicationRepository(db)
        snap = repo.get_portfolio_state_snapshot(
            application_id="app_curr1",
            founder_id="fnd_test1",
            domain_primary="market_economics",
        )
        assert snap["committed_pass_usd_excl_current"] == 100_000.0
        assert snap["same_founder_pass_committed_usd_excl_current"] == 100_000.0
        assert snap["same_founder_pass_count_excl_current"] == 1
        assert snap["domain_pass_count_excl_current"] == 1
        assert snap["open_pipeline_count_excl_current"] == 1
        assert snap["notional_capacity_usd"] == 12_000_000.0
        assert snap["domain_pass_committed_usd_excl_current"] == 100_000.0
        assert snap["dry_powder_usd_excl_current"] == 12_000_000.0 - 100_000.0
        assert snap["liquidity_reserve_floor_usd"] == pytest.approx(12_000_000.0 * 0.05)
        assert snap["portfolio_regime_code"] == "neutral"
        assert snap["portfolio_drawdown_proxy"] == 0.0
    finally:
        db.close()


def test_domain_pass_density_adds_delta():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 50_000, "compliance_status": "clear"}
    ps = {"domain_pass_count_excl_current": 25}
    plain = policy.evaluate(app, _passing_score(ci_lower=0.192), portfolio_state=None)
    crowded = policy.evaluate(app, _passing_score(ci_lower=0.192), portfolio_state=ps)
    assert plain["decision"] == "pass"
    assert crowded["decision"] == "fail"
    assert crowded["portfolio_adjustments"]["cs_required_delta"] >= 0.015
