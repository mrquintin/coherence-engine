"""Tests for extended R(S, portfolio_state): liquidity, domain USD, drawdown, regime."""

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


def test_liquidity_reserve_pressure_manual_review():
    policy = DecisionPolicyService()
    cap = 2_000_000.0
    floor = cap * 0.05
    committed = cap - floor - 50_000.0
    app = {"domain_primary": "market_economics", "requested_check_usd": 100_000, "compliance_status": "clear"}
    ps = {
        "notional_capacity_usd": cap,
        "committed_pass_usd_excl_current": committed,
        "liquidity_reserve_floor_usd": floor,
    }
    out = policy.evaluate(app, _passing_score(ci_lower=0.5), portfolio_state=ps)
    assert out["decision"] == "manual_review"
    assert any(g["reason_code"] == "PORTFOLIO_LIQUIDITY_RESERVE_PRESSURE" for g in out["failed_gates"])
    adj = out["portfolio_adjustments"]
    assert adj["dry_powder_usd_after_request"] == pytest.approx(cap - committed - 100_000.0)


def test_domain_usd_concentration_manual_review():
    policy = DecisionPolicyService()
    cap = 1_000_000.0
    domain_committed = 380_000.0
    requested = 50_000.0
    assert (domain_committed + requested) / cap > 0.42
    app = {"domain_primary": "governance", "requested_check_usd": int(requested), "compliance_status": "clear"}
    ps = {
        "notional_capacity_usd": cap,
        "domain_pass_committed_usd_excl_current": domain_committed,
    }
    out = policy.evaluate(app, _passing_score(ci_lower=0.5), portfolio_state=ps)
    assert out["decision"] == "manual_review"
    assert any(g["reason_code"] == "PORTFOLIO_DOMAIN_USD_CONCENTRATION_HIGH" for g in out["failed_gates"])
    assert out["portfolio_adjustments"]["domain_primary_usd_share"] == pytest.approx(
        (domain_committed + requested) / cap
    )


def test_drawdown_proxy_elevated_manual_review():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 50_000, "compliance_status": "clear"}
    ps = {"portfolio_drawdown_proxy": 0.31}
    out = policy.evaluate(app, _passing_score(ci_lower=0.5), portfolio_state=ps)
    assert out["decision"] == "manual_review"
    assert any(g["reason_code"] == "PORTFOLIO_DRAWDOWN_PROXY_ELEVATED" for g in out["failed_gates"])
    assert out["portfolio_adjustments"]["r_term_audit"]["r_drawdown"] == pytest.approx(0.02)


def test_regime_stress_raises_cs_delta_deterministic():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 50_000, "compliance_status": "clear"}
    base = policy.evaluate(app, _passing_score(ci_lower=0.22), portfolio_state=None)
    stressed = policy.evaluate(
        app,
        _passing_score(ci_lower=0.22),
        portfolio_state={"portfolio_regime_code": "stress"},
    )
    assert base["threshold_required"] < stressed["threshold_required"]
    assert stressed["portfolio_adjustments"]["r_term_audit"]["r_regime"] == pytest.approx(0.015)
    assert stressed["portfolio_adjustments"]["portfolio_regime_code"] == "stress"


def test_unknown_regime_normalizes_to_neutral_no_regime_delta():
    policy = DecisionPolicyService()
    app = {"domain_primary": "market_economics", "requested_check_usd": 50_000, "compliance_status": "clear"}
    a = policy.evaluate(app, _passing_score(), portfolio_state={"portfolio_regime_code": "unknown_xyz"})
    b = policy.evaluate(app, _passing_score(), portfolio_state=None)
    assert a["policy_version"] == b["policy_version"]
    assert a["threshold_required"] == b["threshold_required"]


def test_repository_snapshot_respects_env_regime_and_drawdown(monkeypatch):
    monkeypatch.setenv("COHERENCE_PORTFOLIO_REGIME", "defensive")
    monkeypatch.setenv("COHERENCE_PORTFOLIO_DRAWDOWN_PROXY", "0.14")
    db = SessionLocal()
    try:
        f = models.Founder(
            id="fnd_env1",
            full_name="E",
            email="e@example.com",
            company_name="Co E",
            country="US",
        )
        app = models.Application(
            id="app_env1",
            founder_id="fnd_env1",
            one_liner="e",
            requested_check_usd=10_000,
            use_of_funds_summary="u",
            preferred_channel="web_voice",
            domain_primary="public_health",
            compliance_status="clear",
            status="intake_created",
        )
        db.add_all([f, app])
        db.commit()
        repo = ApplicationRepository(db)
        snap = repo.get_portfolio_state_snapshot(
            application_id="app_env1",
            founder_id="fnd_env1",
            domain_primary="public_health",
        )
        assert snap["portfolio_regime_code"] == "defensive"
        assert snap["portfolio_drawdown_proxy"] == pytest.approx(0.14)
        assert snap["domain_pass_committed_usd_excl_current"] == 0.0
    finally:
        db.close()
        monkeypatch.delenv("COHERENCE_PORTFOLIO_REGIME", raising=False)
        monkeypatch.delenv("COHERENCE_PORTFOLIO_DRAWDOWN_PROXY", raising=False)


def test_domain_usd_share_adds_cs_delta_branch():
    policy = DecisionPolicyService()
    cap = 12_000_000.0
    requested = 100_000.0
    domain_committed = 0.30 * cap - requested
    app = {"domain_primary": "market_economics", "requested_check_usd": int(requested), "compliance_status": "clear"}
    ps = {
        "notional_capacity_usd": cap,
        "domain_pass_committed_usd_excl_current": domain_committed,
    }
    low_domain = policy.evaluate(app, _passing_score(ci_lower=0.22), portfolio_state={"notional_capacity_usd": cap})
    high_domain = policy.evaluate(app, _passing_score(ci_lower=0.22), portfolio_state=ps)
    assert high_domain["threshold_required"] > low_domain["threshold_required"]
    assert high_domain["portfolio_adjustments"]["r_term_audit"]["r_domain_usd"] >= 0.005
