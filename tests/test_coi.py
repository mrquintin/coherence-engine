"""Conflict-of-interest registry + automated gate tests (prompt 59).

Covers:

* :func:`check_coi` against active vs. expired declarations.
* :func:`check_coi` against the various relationship kinds
  (hard ``employed`` / ``family`` / ``invested`` / ``board`` /
  ``founder`` vs. soft ``advisor``).
* :func:`route_for_application` skips conflicted candidates.
* :func:`record_override` rejects justifications below the floor.
* The decision policy ``coi_clear`` gate downgrades ``pass`` to
  ``manual_review`` on ``conflicted`` and ``requires_disclosure``.
* The router ``POST /coi/override`` returns 422 on a missing or
  too-short justification (prompt 59 prohibition: every override
  carries a justification ≥ 50 chars and is audited).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.routers.coi import router as coi_router
from coherence_engine.server.fund.security import FundSecurityMiddleware
from coherence_engine.server.fund.services.conflict_of_interest import (
    COI_CLEAR,
    COI_CONFLICTED,
    COI_REQUIRES_DISCLOSURE,
    MIN_OVERRIDE_JUSTIFICATION_LENGTH,
    COIError,
    check_coi,
    record_override,
    route_for_application,
)
from coherence_engine.server.fund.services.decision_policy import (
    DecisionPolicyService,
)


_LONG_JUSTIFICATION = (
    "Partner served on the prior advisory board for a three-month "
    "engagement that concluded in 2024; relationship has been disclosed "
    "to the IC and counsel reviewed the conflict on 2026-04-01."
)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _seed_application(
    db,
    *,
    application_id: str = "app_coi1",
    founder_id: str = "fnd_coi1",
    company_name: str = "Acme Robotics",
) -> Dict[str, Any]:
    founder = models.Founder(
        id=founder_id,
        full_name="Test Founder",
        email=f"{founder_id}@example.com",
        country="US",
        company_name=company_name,
    )
    application = models.Application(
        id=application_id,
        founder_id=founder_id,
        one_liner="COI gate pilot",
        requested_check_usd=200_000,
        use_of_funds_summary="Seed",
        preferred_channel="web_voice",
        domain_primary="market_economics",
        compliance_status="clear",
        status="scoring_in_progress",
    )
    db.add_all([founder, application])
    db.commit()
    return {
        "id": application_id,
        "founder_id": founder_id,
        "company_name": company_name,
    }


def _seed_declaration(
    db,
    *,
    partner_id: str,
    party_id_ref: str,
    relationship: str = "employed",
    period_start: datetime = None,
    period_end: datetime = None,
    party_kind: str = "company",
) -> models.COIDeclaration:
    period_start = period_start or (_utc_now() - timedelta(days=30))
    decl = models.COIDeclaration(
        id=f"coid_test_{partner_id}_{party_id_ref}_{relationship}",
        partner_id=partner_id,
        party_kind=party_kind,
        party_id_ref=party_id_ref,
        relationship=relationship,
        period_start=period_start,
        period_end=period_end,
        evidence_uri="",
        note="",
        status="active",
    )
    db.add(decl)
    db.commit()
    return decl


# ---------------------------------------------------------------------------
# check_coi: active declaration fires
# ---------------------------------------------------------------------------


def test_active_declaration_fires_coi_gate():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        _seed_declaration(
            db,
            partner_id="ptnr_alice",
            party_id_ref=app["company_name"],
            relationship="employed",
        )

        result = check_coi(db, app, "ptnr_alice")
        db.commit()

        assert result.status == COI_CONFLICTED
        assert result.coi_clear is False
        assert any(
            ev["relationship"] == "employed" for ev in result.evidence
        )

        rows = db.query(models.COICheck).all()
        assert len(rows) == 1
        assert rows[0].status == COI_CONFLICTED
    finally:
        db.close()


def test_active_declaration_against_founder_id_fires():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        _seed_declaration(
            db,
            partner_id="ptnr_bob",
            party_id_ref=app["founder_id"],
            relationship="family",
            party_kind="person",
        )

        result = check_coi(db, app, "ptnr_bob")
        assert result.status == COI_CONFLICTED
        assert result.evidence[0]["relationship"] == "family"
    finally:
        db.close()


def test_advisor_relationship_requires_disclosure():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        _seed_declaration(
            db,
            partner_id="ptnr_carol",
            party_id_ref=app["company_name"],
            relationship="advisor",
        )

        result = check_coi(db, app, "ptnr_carol")
        assert result.status == COI_REQUIRES_DISCLOSURE
        # No override / disclosure attached → not yet clear.
        assert result.coi_clear is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# check_coi: expired / revoked declarations are silent
# ---------------------------------------------------------------------------


def test_expired_declaration_is_clear():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        # Declaration that ended a year ago.
        _seed_declaration(
            db,
            partner_id="ptnr_dave",
            party_id_ref=app["company_name"],
            relationship="employed",
            period_start=_utc_now() - timedelta(days=720),
            period_end=_utc_now() - timedelta(days=365),
        )

        result = check_coi(db, app, "ptnr_dave")
        assert result.status == COI_CLEAR
        assert result.evidence == []
        assert result.coi_clear is True
    finally:
        db.close()


def test_revoked_declaration_is_clear():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        decl = _seed_declaration(
            db,
            partner_id="ptnr_erin",
            party_id_ref=app["company_name"],
            relationship="employed",
        )
        decl.status = "revoked"
        db.commit()

        result = check_coi(db, app, "ptnr_erin")
        assert result.status == COI_CLEAR
    finally:
        db.close()


def test_no_declarations_is_clear():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        result = check_coi(db, app, "ptnr_unknown")
        assert result.status == COI_CLEAR
        assert result.coi_clear is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# route_for_application: never auto-routes a conflicted application
# ---------------------------------------------------------------------------


def test_route_skips_conflicted_partner_picks_clear_one():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        _seed_declaration(
            db,
            partner_id="ptnr_first",
            party_id_ref=app["company_name"],
            relationship="invested",
        )

        chosen, results = route_for_application(
            db, app, ["ptnr_first", "ptnr_second"]
        )
        assert chosen == "ptnr_second"
        assert results[0].status == COI_CONFLICTED
        assert results[1].status == COI_CLEAR
    finally:
        db.close()


def test_route_returns_none_when_all_partners_conflict():
    db = SessionLocal()
    try:
        app = _seed_application(db)
        for partner_id in ("ptnr_a", "ptnr_b"):
            _seed_declaration(
                db,
                partner_id=partner_id,
                party_id_ref=app["company_name"],
                relationship="board",
            )
        chosen, results = route_for_application(db, app, ["ptnr_a", "ptnr_b"])
        assert chosen is None
        assert all(r.status == COI_CONFLICTED for r in results)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# decision_policy gate
# ---------------------------------------------------------------------------


def _passing_score(ci_lower: float = 0.8) -> dict:
    return {
        "transcript_quality_score": 0.95,
        "anti_gaming_score": 0.1,
        "coherence_superiority_ci95": {
            "lower": ci_lower,
            "upper": min(ci_lower + 0.1, 0.99),
        },
    }


def test_decision_policy_coi_conflict_downgrades_pass_to_manual_review():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "coi_clear": False,
        "coi_status": "conflicted",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "manual_review"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "COI_CONFLICT" in codes


def test_decision_policy_coi_disclosure_required_downgrades_pass():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "coi_clear": False,
        "coi_status": "requires_disclosure",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "manual_review"
    codes = {g["reason_code"] for g in out["failed_gates"]}
    assert "COI_DISCLOSURE_REQUIRED" in codes


def test_decision_policy_coi_clear_true_does_not_fire_gate():
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
        "coi_clear": True,
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "pass"


def test_decision_policy_coi_field_missing_is_silent_backcompat():
    # Backward-compat: no ``coi_clear`` field → no COI gate fires.
    policy = DecisionPolicyService()
    app = {
        "domain_primary": "market_economics",
        "requested_check_usd": 50_000,
        "compliance_status": "clear",
    }
    out = policy.evaluate(app, _passing_score())
    assert out["decision"] == "pass"


# ---------------------------------------------------------------------------
# record_override service: justification floor
# ---------------------------------------------------------------------------


def test_record_override_rejects_short_justification_service_layer():
    db = SessionLocal()
    try:
        with pytest.raises(COIError) as exc:
            record_override(
                db,
                application_id="app_coi1",
                partner_id="ptnr_x",
                justification="too short",
                overridden_by="admin@test",
            )
        assert exc.value.code == "JUSTIFICATION_TOO_SHORT"
    finally:
        db.close()


def test_record_override_accepts_long_justification_and_persists():
    db = SessionLocal()
    try:
        row = record_override(
            db,
            application_id="app_coi1",
            partner_id="ptnr_x",
            justification=_LONG_JUSTIFICATION,
            overridden_by="admin@test",
        )
        db.commit()
        assert row.justification.startswith("Partner served")
        assert len(row.justification) >= MIN_OVERRIDE_JUSTIFICATION_LENGTH
        assert row.overridden_by == "admin@test"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Router: POST /coi/override 422 on missing justification (prompt 59)
# ---------------------------------------------------------------------------


@pytest.fixture
def coi_test_client():
    """Mount the coi router on a minimal FastAPI app with the security middleware."""
    os.environ["COHERENCE_FUND_AUTH_MODE"] = "db"
    os.environ["COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED"] = "false"
    os.environ["COHERENCE_FUND_SECRET_MANAGER_PROVIDER"] = "disabled"
    os.environ["COHERENCE_FUND_RATE_LIMIT_ENABLED"] = "false"

    from coherence_engine.server.fund.repositories.api_key_repository import (
        ApiKeyRepository,
    )
    from coherence_engine.server.fund.services.api_key_service import (
        ApiKeyService,
    )

    db = SessionLocal()
    tokens: Dict[str, str] = {}
    try:
        repo = ApiKeyRepository(db)
        svc = ApiKeyService()
        admin = svc.create_key(
            repo,
            label="coi-admin",
            role="admin",
            created_by="tests",
            expires_in_days=30,
        )
        partner = svc.create_key(
            repo,
            label="coi-partner",
            role="partner",
            created_by="tests",
            expires_in_days=30,
        )
        tokens["admin"] = admin["token"]
        tokens["partner"] = partner["token"]
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.add_middleware(FundSecurityMiddleware)
    app.include_router(coi_router)
    client = TestClient(app)
    return client, tokens


def _hdr(token: str) -> dict:
    return {"X-API-Key": token, "X-Request-Id": "req_coi_test"}


def test_router_override_rejects_missing_justification_with_422(
    coi_test_client,
):
    client, tokens = coi_test_client
    resp = client.post(
        "/coi/override",
        headers=_hdr(tokens["admin"]),
        json={
            "application_id": "app_coi_router",
            "partner_id": "ptnr_x",
            # no justification field at all
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "JUSTIFICATION_TOO_SHORT"


def test_router_override_rejects_short_justification_with_422(coi_test_client):
    client, tokens = coi_test_client
    resp = client.post(
        "/coi/override",
        headers=_hdr(tokens["admin"]),
        json={
            "application_id": "app_coi_router",
            "partner_id": "ptnr_x",
            "justification": "too short for audit trail",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "JUSTIFICATION_TOO_SHORT"


def test_router_override_accepts_long_justification(coi_test_client):
    client, tokens = coi_test_client
    resp = client.post(
        "/coi/override",
        headers=_hdr(tokens["admin"]),
        json={
            "application_id": "app_coi_router",
            "partner_id": "ptnr_x",
            "justification": _LONG_JUSTIFICATION,
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["partner_id"] == "ptnr_x"
    assert data["justification_chars"] >= MIN_OVERRIDE_JUSTIFICATION_LENGTH


def test_router_override_forbidden_for_partner_role(coi_test_client):
    client, tokens = coi_test_client
    resp = client.post(
        "/coi/override",
        headers=_hdr(tokens["partner"]),
        json={
            "application_id": "app_coi_router",
            "partner_id": "ptnr_x",
            "justification": _LONG_JUSTIFICATION,
        },
    )
    assert resp.status_code == 403


def test_router_declare_creates_declaration(coi_test_client):
    client, tokens = coi_test_client
    resp = client.post(
        "/coi/declarations",
        headers=_hdr(tokens["admin"]),
        json={
            "partner_id": "ptnr_router",
            "party_kind": "company",
            "party_id_ref": "Acme Robotics",
            "relationship": "employed",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["relationship"] == "employed"
    assert data["partner_id"] == "ptnr_router"
