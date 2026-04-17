"""Formalization checks for the decision policy specification + version pin."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services.decision_policy import DECISION_POLICY_VERSION


SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "specs"
    / "decision_policy_spec.md"
)


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_decision_policy_version_constant_is_pinned_to_v1():
    assert DECISION_POLICY_VERSION == "decision-policy-v1"


def test_decision_policy_spec_document_exists_and_declares_v1():
    assert SPEC_PATH.exists(), f"decision policy spec missing at {SPEC_PATH}"
    text = SPEC_PATH.read_text(encoding="utf-8")
    assert "decision-policy-v1" in text
    assert "schema_version" in text


def test_inserted_decision_row_persists_decision_policy_version():
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_spec1",
            full_name="Spec Founder",
            email="spec@example.com",
            company_name="Spec Co",
            country="US",
        )
        app = models.Application(
            id="app_spec1",
            founder_id="fnd_spec1",
            one_liner="spec test",
            requested_check_usd=100_000,
            use_of_funds_summary="hiring",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="scoring_in_progress",
        )
        decision = models.Decision(
            id="dec_spec1",
            application_id="app_spec1",
            decision="pass",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="params_starter_v1",
            threshold_required=0.2,
            coherence_observed=0.3,
            margin=0.1,
            failed_gates_json="[]",
        )
        db.add_all([founder, app, decision])
        db.commit()

        reloaded = (
            db.query(models.Decision)
            .filter(models.Decision.id == "dec_spec1")
            .one()
        )
        assert reloaded.decision_policy_version == DECISION_POLICY_VERSION
    finally:
        db.close()


def test_spec_mentions_r_term_code_symbols():
    text = SPEC_PATH.read_text(encoding="utf-8")
    for symbol in (
        "r_utilization",
        "r_domain_count",
        "r_domain_usd",
        "r_pipeline",
        "r_liquidity",
        "r_drawdown",
        "r_regime",
    ):
        assert symbol in text, f"spec must reference R() code symbol {symbol}"
