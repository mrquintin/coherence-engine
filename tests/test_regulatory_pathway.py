"""Regulatory-pathway classifier + decision-policy gate tests (prompt 56).

Covers:

* Classifier matches a single eligible pathway and returns ``clear``
  when all prerequisites (counsel signoff freshness, investor
  verification) are satisfied.
* 506(c) requires ``investor_verification_status="verified"``;
  missing -> ``unclear`` -> decision policy ``manual_review`` with
  reason ``REGULATORY_PATHWAY_UNCLEAR``.
* Reg S applies to non-US founders and routes per the YAML when
  the operator declares advertising mode.
* Ambiguity (zero or multiple matches) -> ``ambiguous`` -> decision
  policy ``manual_review`` with reason ``REGULATORY_PATHWAY_AMBIGUOUS``.
* Stale counsel signoff -> ``unclear`` even if investor requirement
  is satisfied.
* Backwards compatibility: when the application omits
  ``regulatory_pathway_status`` the gate is silent.

The tests construct an in-memory :class:`PathwayRegistry` so they do
not depend on the on-disk YAML default; a separate test verifies the
default file loads cleanly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coherence_engine.server.fund.services.decision_policy import (
    DecisionPolicyService,
)
from coherence_engine.server.fund.services.regulatory_pathway import (
    Pathway,
    PathwayRegistry,
    REGULATORY_PATHWAY_SCHEMA_VERSION,
    RegulatoryPathwayError,
    classify,
    load_pathway_registry,
    regulatory_pathway_clear,
)


_NOW = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
_FRESH_SIGNOFF = _NOW - timedelta(days=10)
_STALE_SIGNOFF = _NOW - timedelta(days=120)


def _pathway(
    pid: str,
    *,
    jurisdiction: str = "US",
    investor_requirement: str = "none",
    advertising: str = "permitted",
    counsel_signoff_required: bool = True,
    counsel_signoff_at=_FRESH_SIGNOFF,
    counsel_signoff_by: str = "Acme LLP",
    max_investors=None,
) -> Pathway:
    return Pathway(
        id=pid,
        jurisdiction=jurisdiction,
        investor_requirement=investor_requirement,
        advertising=advertising,
        max_investors=max_investors,
        integration_window_days=30,
        counsel_signoff_required=counsel_signoff_required,
        counsel_signoff_at=counsel_signoff_at,
        counsel_signoff_by=counsel_signoff_by,
    )


def _registry(*pathways: Pathway, ttl_days: int = 90) -> PathwayRegistry:
    return PathwayRegistry(
        schema_version=REGULATORY_PATHWAY_SCHEMA_VERSION,
        pathways=tuple(pathways),
        counsel_signoff_ttl_days=ttl_days,
    )


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def test_default_yaml_loads_with_expected_pathways():
    reg = load_pathway_registry()
    assert reg.schema_version == REGULATORY_PATHWAY_SCHEMA_VERSION
    ids = {p.id for p in reg.pathways}
    assert {"reg_d_506b", "reg_d_506c", "reg_cf", "reg_s"}.issubset(ids)
    p506c = reg.by_id("reg_d_506c")
    assert p506c is not None
    assert p506c.investor_requirement == "accredited_verified"
    assert p506c.advertising == "permitted"
    p506b = reg.by_id("reg_d_506b")
    assert p506b is not None
    assert p506b.advertising == "prohibited"
    pregs = reg.by_id("reg_s")
    assert pregs is not None and pregs.jurisdiction == "non_US"


def test_loader_rejects_wrong_schema_version(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: regulatory-pathways-v999\n"
        "pathways: []\n",
        encoding="utf-8",
    )
    with pytest.raises(RegulatoryPathwayError):
        load_pathway_registry(bad)


def test_loader_rejects_unknown_jurisdiction(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: regulatory-pathways-v1\n"
        "pathways:\n"
        "  - id: reg_x\n"
        "    jurisdiction: MARS\n"
        "    investor_requirement: none\n"
        "    advertising: permitted\n"
        "    counsel_signoff_required: false\n"
        "    integration_window_days: 30\n",
        encoding="utf-8",
    )
    with pytest.raises(RegulatoryPathwayError):
        load_pathway_registry(bad)


def test_loader_rejects_duplicate_pathway_ids(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: regulatory-pathways-v1\n"
        "pathways:\n"
        "  - id: reg_d_506c\n"
        "    jurisdiction: US\n"
        "    investor_requirement: accredited_verified\n"
        "    advertising: permitted\n"
        "    counsel_signoff_required: false\n"
        "    integration_window_days: 30\n"
        "  - id: reg_d_506c\n"
        "    jurisdiction: US\n"
        "    investor_requirement: accredited_verified\n"
        "    advertising: permitted\n"
        "    counsel_signoff_required: false\n"
        "    integration_window_days: 30\n",
        encoding="utf-8",
    )
    with pytest.raises(RegulatoryPathwayError):
        load_pathway_registry(bad)


# ---------------------------------------------------------------------------
# Classifier — happy paths
# ---------------------------------------------------------------------------


def test_506c_clear_when_accredited_and_signoff_fresh():
    reg = _registry(
        _pathway(
            "reg_d_506c",
            investor_requirement="accredited_verified",
            advertising="permitted",
        )
    )
    app = {
        "founder_country": "US",
        "advertising_mode": "permitted",
        "investor_verification_status": "verified",
    }
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "clear"
    assert match.pathway_id == "reg_d_506c"
    assert match.reason == ""
    assert regulatory_pathway_clear(app, registry=reg, now=_NOW) is True


def test_reg_s_clear_for_non_us_founder():
    reg = _registry(
        _pathway(
            "reg_s",
            jurisdiction="non_US",
            investor_requirement="none",
            advertising="permitted",
        )
    )
    app = {
        "founder_country": "DE",
        "advertising_mode": "permitted",
    }
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "clear"
    assert match.pathway_id == "reg_s"


def test_506b_clear_with_self_certified_investor_and_no_advertising():
    reg = _registry(
        _pathway(
            "reg_d_506b",
            investor_requirement="self_certified",
            advertising="prohibited",
            max_investors=35,
        )
    )
    app = {
        "founder_country": "US",
        "advertising_mode": "prohibited",
        "investor_verification_status": "self_certified",
    }
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "clear"
    assert match.pathway_id == "reg_d_506b"


# ---------------------------------------------------------------------------
# Classifier — unclear (single match, prerequisite missing)
# ---------------------------------------------------------------------------


def test_506c_unclear_when_accredited_verification_missing():
    reg = _registry(
        _pathway(
            "reg_d_506c",
            investor_requirement="accredited_verified",
            advertising="permitted",
        )
    )
    app = {
        "founder_country": "US",
        "advertising_mode": "permitted",
        # no investor_verification_status -> defaults to "absent"
    }
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "unclear"
    assert match.pathway_id == "reg_d_506c"
    assert match.reason == "REGULATORY_PATHWAY_UNCLEAR"


def test_unclear_when_counsel_signoff_stale():
    reg = _registry(
        _pathway(
            "reg_d_506c",
            investor_requirement="accredited_verified",
            advertising="permitted",
            counsel_signoff_at=_STALE_SIGNOFF,
        )
    )
    app = {
        "founder_country": "US",
        "advertising_mode": "permitted",
        "investor_verification_status": "verified",
    }
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "unclear"
    assert match.pathway_id == "reg_d_506c"


def test_unclear_when_counsel_signoff_missing():
    reg = _registry(
        _pathway(
            "reg_d_506c",
            investor_requirement="accredited_verified",
            advertising="permitted",
            counsel_signoff_at=None,
            counsel_signoff_by="",
        )
    )
    app = {
        "founder_country": "US",
        "advertising_mode": "permitted",
        "investor_verification_status": "verified",
    }
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "unclear"


# ---------------------------------------------------------------------------
# Classifier — ambiguous (zero or multiple matches)
# ---------------------------------------------------------------------------


def test_ambiguous_when_no_pathway_matches_jurisdiction():
    reg = _registry(_pathway("reg_d_506c", investor_requirement="accredited_verified"))
    app = {"founder_country": "DE", "advertising_mode": "permitted"}
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "ambiguous"
    assert match.pathway_id is None
    assert match.reason == "REGULATORY_PATHWAY_AMBIGUOUS"


def test_ambiguous_when_advertising_mode_unspecified_and_multiple_pathways():
    reg = _registry(
        _pathway("reg_d_506c", advertising="permitted"),
        _pathway(
            "reg_d_506b",
            investor_requirement="self_certified",
            advertising="prohibited",
        ),
    )
    app = {"founder_country": "US"}  # advertising mode unspecified
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "ambiguous"
    assert "reg_d_506c" in match.candidates
    assert "reg_d_506b" in match.candidates


def test_ambiguous_when_jurisdiction_missing():
    reg = _registry(_pathway("reg_d_506c"))
    app = {"advertising_mode": "permitted"}
    match = classify(app, registry=reg, now=_NOW)
    assert match.status == "ambiguous"
    assert match.pathway_id is None


# ---------------------------------------------------------------------------
# Decision-policy gate integration
# ---------------------------------------------------------------------------


def _passing_score() -> dict:
    return {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.1,
        "coherence_superiority_ci95": {"lower": 0.8, "upper": 0.9},
    }


def test_decision_policy_gate_silent_when_pathway_status_absent():
    """Backwards compatibility: callers that don't thread the field pass."""
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "pass"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "REGULATORY_PATHWAY_UNCLEAR" not in codes
    assert "REGULATORY_PATHWAY_AMBIGUOUS" not in codes


def test_decision_policy_gate_clear_status_passes():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "regulatory_pathway_status": "clear",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "pass"


def test_decision_policy_gate_unclear_downgrades_to_manual_review():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "regulatory_pathway_status": "unclear",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "manual_review"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "REGULATORY_PATHWAY_UNCLEAR" in codes


def test_decision_policy_gate_ambiguous_downgrades_to_manual_review():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "regulatory_pathway_status": "ambiguous",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "manual_review"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "REGULATORY_PATHWAY_AMBIGUOUS" in codes


def test_decision_policy_unknown_status_value_treated_as_unclear():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "regulatory_pathway_status": "garbage",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "manual_review"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "REGULATORY_PATHWAY_UNCLEAR" in codes


# ---------------------------------------------------------------------------
# Application model column smoke test
# ---------------------------------------------------------------------------


def test_application_model_persists_regulatory_pathway_id():
    """Round-trip the new ``regulatory_pathway_id`` column."""
    import os

    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    from coherence_engine.server.fund import models
    from coherence_engine.server.fund.database import (
        Base,
        SessionLocal,
        engine,
    )

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_reg1",
            full_name="Reg Founder",
            email="reg@example.com",
            company_name="Reg Co",
            country="US",
        )
        app = models.Application(
            id="app_reg1",
            founder_id="fnd_reg1",
            one_liner="reg-pathway test",
            requested_check_usd=100_000,
            use_of_funds_summary="hiring",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="scoring_in_progress",
            regulatory_pathway_id="reg_d_506c",
        )
        db.add_all([founder, app])
        db.commit()

        reloaded = (
            db.query(models.Application)
            .filter(models.Application.id == "app_reg1")
            .one()
        )
        assert reloaded.regulatory_pathway_id == "reg_d_506c"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
