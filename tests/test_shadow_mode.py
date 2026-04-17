"""Shadow-mode evaluation + ``scoring_mode`` enforcement tests (prompt 12)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.repositories.application_repository import (
    ApplicationRepository,
)
from coherence_engine.server.fund.services.application_service import (
    SHADOW_ARTIFACT_KIND,
    ApplicationService,
)
from coherence_engine.server.fund.services.event_publisher import (
    SCORING_MODE_ENFORCE,
    SCORING_MODE_SHADOW,
    EventPublisher,
    is_shadow_event_payload,
    tag_payload_with_mode,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Model + repository round-trip
# ---------------------------------------------------------------------------


def _create_application(
    db,
    *,
    app_id: str,
    founder_id: str,
    scoring_mode: str = SCORING_MODE_ENFORCE,
    one_liner: str = "Workflow automation for SMB finance ops.",
) -> models.Application:
    founder = models.Founder(
        id=founder_id,
        full_name="Shadow Tester",
        email=f"{founder_id}@example.com",
        company_name="Shadow Co",
        country="US",
    )
    app = models.Application(
        id=app_id,
        founder_id=founder_id,
        one_liner=one_liner,
        requested_check_usd=50_000,
        use_of_funds_summary="hire and pilot",
        preferred_channel="web_voice",
        transcript_text=(
            "We reduce back-office processing time for small businesses. "
            "Our software integrates accounting, invoicing, and procurement. "
            "Pilot users reported fewer reconciliation errors and faster closes. "
            "The market has millions of SMBs with fragmented workflows. "
            "We sell a subscription model with expansion to payments."
        ),
        domain_primary="market_economics",
        compliance_status="clear",
        status="scoring_in_progress",
        scoring_mode=scoring_mode,
    )
    db.add_all([founder, app])
    db.flush()
    return app


def test_scoring_mode_column_defaults_to_enforce():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_shadow_default", founder_id="fnd_shadow_1")
        db.commit()
        row = db.query(models.Application).filter_by(id="app_shadow_default").one()
        assert row.scoring_mode == SCORING_MODE_ENFORCE
    finally:
        db.close()


def test_scoring_mode_column_accepts_shadow():
    db = SessionLocal()
    try:
        _create_application(
            db,
            app_id="app_shadow_explicit",
            founder_id="fnd_shadow_2",
            scoring_mode=SCORING_MODE_SHADOW,
        )
        db.commit()
        row = db.query(models.Application).filter_by(id="app_shadow_explicit").one()
        assert row.scoring_mode == SCORING_MODE_SHADOW
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Event-publisher helpers
# ---------------------------------------------------------------------------


def test_tag_payload_with_mode_attaches_mode_field():
    out = tag_payload_with_mode({"application_id": "app_1"}, SCORING_MODE_SHADOW)
    assert out["mode"] == SCORING_MODE_SHADOW
    assert out["application_id"] == "app_1"
    assert is_shadow_event_payload(out) is True


def test_tag_payload_with_mode_rejects_unknown_value():
    with pytest.raises(ValueError):
        tag_payload_with_mode({}, "bogus")


def test_is_shadow_event_payload_defaults_to_enforce():
    assert is_shadow_event_payload({}) is False
    assert is_shadow_event_payload({"mode": "enforce"}) is False


# ---------------------------------------------------------------------------
# set_scoring_mode transitions
# ---------------------------------------------------------------------------


def _service(db) -> ApplicationService:
    repo = ApplicationRepository(db)
    events = EventPublisher(db, strict_events=False)
    return ApplicationService(repo, events)


def test_set_scoring_mode_enforce_to_shadow_before_decision_is_allowed():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_shadow_01", founder_id="fnd_shadow_3")
        db.commit()
        svc = _service(db)
        result = svc.set_scoring_mode(
            application_id="app_shadow_01",
            new_mode=SCORING_MODE_SHADOW,
        )
        db.commit()
        assert result["previous_mode"] == SCORING_MODE_ENFORCE
        assert result["new_mode"] == SCORING_MODE_SHADOW
        assert result["changed"] is True
        row = db.query(models.Application).filter_by(id="app_shadow_01").one()
        assert row.scoring_mode == SCORING_MODE_SHADOW
    finally:
        db.close()


def test_set_scoring_mode_is_idempotent_when_mode_is_unchanged():
    db = SessionLocal()
    try:
        _create_application(
            db,
            app_id="app_shadow_02",
            founder_id="fnd_shadow_4",
            scoring_mode=SCORING_MODE_SHADOW,
        )
        db.commit()
        svc = _service(db)
        result = svc.set_scoring_mode(
            application_id="app_shadow_02",
            new_mode=SCORING_MODE_SHADOW,
        )
        db.commit()
        assert result["changed"] is False
        assert result["previous_mode"] == SCORING_MODE_SHADOW
        assert result["new_mode"] == SCORING_MODE_SHADOW
    finally:
        db.close()


def test_set_scoring_mode_rejects_enforce_to_shadow_after_decision_without_force():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_shadow_03", founder_id="fnd_shadow_5")
        dec = models.Decision(
            id="dec_shadow_03",
            application_id="app_shadow_03",
            decision="pass",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="default",
            threshold_required=0.18,
            coherence_observed=0.25,
            margin=0.07,
            failed_gates_json="[]",
        )
        db.add(dec)
        db.commit()
        svc = _service(db)
        with pytest.raises(RuntimeError) as excinfo:
            svc.set_scoring_mode(
                application_id="app_shadow_03",
                new_mode=SCORING_MODE_SHADOW,
            )
        assert "enforce_to_shadow_forbidden_after_decision_issued" in str(excinfo.value)
        row = db.query(models.Application).filter_by(id="app_shadow_03").one()
        assert row.scoring_mode == SCORING_MODE_ENFORCE
    finally:
        db.close()


def test_set_scoring_mode_allows_enforce_to_shadow_after_decision_with_force():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_shadow_04", founder_id="fnd_shadow_6")
        dec = models.Decision(
            id="dec_shadow_04",
            application_id="app_shadow_04",
            decision="pass",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="default",
            threshold_required=0.18,
            coherence_observed=0.25,
            margin=0.07,
            failed_gates_json="[]",
        )
        db.add(dec)
        db.commit()
        svc = _service(db)
        result = svc.set_scoring_mode(
            application_id="app_shadow_04",
            new_mode=SCORING_MODE_SHADOW,
            force=True,
        )
        db.commit()
        assert result["changed"] is True
        assert result["new_mode"] == SCORING_MODE_SHADOW
    finally:
        db.close()


def test_set_scoring_mode_shadow_to_enforce_is_always_allowed():
    """Promoting a reviewed shadow application to enforce must always be
    permitted (that is the prompt's "promote after review" flow and it
    never retroactively hides any past side effect).
    """
    db = SessionLocal()
    try:
        _create_application(
            db,
            app_id="app_shadow_05",
            founder_id="fnd_shadow_7",
            scoring_mode=SCORING_MODE_SHADOW,
        )
        dec = models.Decision(
            id="dec_shadow_05",
            application_id="app_shadow_05",
            decision="manual_review",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="default",
            threshold_required=0.18,
            coherence_observed=0.15,
            margin=-0.03,
            failed_gates_json="[]",
        )
        db.add(dec)
        db.commit()
        svc = _service(db)
        result = svc.set_scoring_mode(
            application_id="app_shadow_05",
            new_mode=SCORING_MODE_ENFORCE,
        )
        db.commit()
        assert result["changed"] is True
        assert result["new_mode"] == SCORING_MODE_ENFORCE
    finally:
        db.close()


def test_set_scoring_mode_rejects_unknown_mode():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_shadow_06", founder_id="fnd_shadow_8")
        db.commit()
        svc = _service(db)
        with pytest.raises(ValueError):
            svc.set_scoring_mode(application_id="app_shadow_06", new_mode="bogus")
    finally:
        db.close()


def test_set_scoring_mode_rejects_missing_application():
    db = SessionLocal()
    try:
        svc = _service(db)
        with pytest.raises(ValueError):
            svc.set_scoring_mode(
                application_id="app_does_not_exist",
                new_mode=SCORING_MODE_SHADOW,
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# End-to-end: shadow pipeline yields shadow_decision_artifact + mode=shadow event
# ---------------------------------------------------------------------------


def _run_worker_for_application(app_id: str, *, scoring_mode: str) -> dict:
    """Create a founder/application/scoring-job and process exactly one worker
    iteration, returning the process_next_scoring_job result dict.
    """
    db = SessionLocal()
    try:
        _create_application(
            db,
            app_id=app_id,
            founder_id=f"fnd_{app_id}",
            scoring_mode=scoring_mode,
        )
        job = models.ScoringJob(
            id=f"sjob_{app_id}",
            application_id=app_id,
            mode="standard",
            dry_run=False,
            trace_id=f"trc_{app_id}",
            idempotency_key=f"idem_{app_id}",
            status="queued",
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        svc = _service(db)
        result = svc.process_next_scoring_job(worker_id="shadow-tests", lease_seconds=60)
        db.commit()
        assert result is not None, "worker returned no result"
        return result
    finally:
        db.close()


def _fetch_decision_issued_payloads(db, app_id: str) -> list[dict]:
    rows = (
        db.query(models.EventOutbox)
        .filter(models.EventOutbox.event_type == "DecisionIssued")
        .all()
    )
    payloads = [json.loads(r.payload_json) for r in rows]
    return [p for p in payloads if p.get("application_id") == app_id]


def test_shadow_application_persists_shadow_decision_artifact():
    result = _run_worker_for_application("app_shadow_pipe_01", scoring_mode=SCORING_MODE_SHADOW)
    assert result["status"] == "completed"
    assert result["scoring_mode"] == SCORING_MODE_SHADOW

    db = SessionLocal()
    try:
        artifacts = (
            db.query(models.ArgumentArtifact)
            .filter(models.ArgumentArtifact.application_id == "app_shadow_pipe_01")
            .all()
        )
        kinds = {a.kind for a in artifacts}
        assert SHADOW_ARTIFACT_KIND in kinds
        # The production-kind artifact MUST NOT be persisted in shadow mode.
        from coherence_engine.server.fund.services.decision_artifact import ARTIFACT_KIND

        assert ARTIFACT_KIND not in kinds
        shadow_rows = [a for a in artifacts if a.kind == SHADOW_ARTIFACT_KIND]
        assert len(shadow_rows) == 1
        payload = json.loads(shadow_rows[0].payload_json)
        assert payload["artifact_kind"] == "decision_artifact"  # the bundle itself is v1
    finally:
        db.close()


def test_shadow_application_emits_decision_issued_with_mode_shadow():
    _run_worker_for_application("app_shadow_pipe_02", scoring_mode=SCORING_MODE_SHADOW)
    db = SessionLocal()
    try:
        payloads = _fetch_decision_issued_payloads(db, "app_shadow_pipe_02")
        assert len(payloads) == 1, f"expected exactly one DecisionIssued, got {len(payloads)}"
        assert payloads[0]["mode"] == SCORING_MODE_SHADOW
        assert is_shadow_event_payload(payloads[0]) is True
    finally:
        db.close()


def test_enforce_application_emits_decision_issued_with_mode_enforce():
    _run_worker_for_application("app_shadow_pipe_03", scoring_mode=SCORING_MODE_ENFORCE)
    db = SessionLocal()
    try:
        payloads = _fetch_decision_issued_payloads(db, "app_shadow_pipe_03")
        assert len(payloads) == 1
        assert payloads[0]["mode"] == SCORING_MODE_ENFORCE

        artifacts = (
            db.query(models.ArgumentArtifact)
            .filter(models.ArgumentArtifact.application_id == "app_shadow_pipe_03")
            .all()
        )
        kinds = {a.kind for a in artifacts}
        from coherence_engine.server.fund.services.decision_artifact import ARTIFACT_KIND

        assert ARTIFACT_KIND in kinds
        assert SHADOW_ARTIFACT_KIND not in kinds
    finally:
        db.close()


def test_shadow_application_does_not_enqueue_founder_notified_event():
    _run_worker_for_application("app_shadow_pipe_04", scoring_mode=SCORING_MODE_SHADOW)
    db = SessionLocal()
    try:
        # Scope to this application's trace_id so we aren't fooled by
        # noise from any cross-test leakage (the fixture also drops/
        # recreates the schema).
        rows = (
            db.query(models.EventOutbox)
            .filter(
                models.EventOutbox.event_type == "FounderNotified",
                models.EventOutbox.trace_id == "trc_app_shadow_pipe_04",
            )
            .all()
        )
        assert rows == [], (
            "FounderNotified must not be emitted for shadow-mode applications; "
            f"saw {[r.idempotency_key for r in rows]}"
        )
    finally:
        db.close()


def test_shadow_application_blocks_escalation_packet_creation():
    _run_worker_for_application("app_shadow_pipe_05", scoring_mode=SCORING_MODE_SHADOW)
    db = SessionLocal()
    try:
        svc = _service(db)
        with pytest.raises(RuntimeError) as excinfo:
            svc.create_escalation_packet(
                application_id="app_shadow_pipe_05",
                partner_email="partners@example.com",
                include_calendar_link=True,
            )
        assert "escalation_forbidden_in_shadow_mode" in str(excinfo.value)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    parent = str(REPO_ROOT.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = parent + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "coherence_engine", "application", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=60,
    )


def test_cli_set_mode_happy_path_enforce_to_shadow():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_cli_01", founder_id="fnd_cli_1")
        db.commit()
    finally:
        db.close()

    proc = _run_cli("set-mode", "--application-id", "app_cli_01", "--mode", "shadow")
    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["application_id"] == "app_cli_01"
    assert payload["previous_mode"] == SCORING_MODE_ENFORCE
    assert payload["new_mode"] == SCORING_MODE_SHADOW
    assert payload["changed"] is True

    db = SessionLocal()
    try:
        row = db.query(models.Application).filter_by(id="app_cli_01").one()
        assert row.scoring_mode == SCORING_MODE_SHADOW
    finally:
        db.close()


def test_cli_set_mode_forbidden_transition_without_force_exits_two():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_cli_02", founder_id="fnd_cli_2")
        dec = models.Decision(
            id="dec_cli_02",
            application_id="app_cli_02",
            decision="pass",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="default",
            threshold_required=0.18,
            coherence_observed=0.25,
            margin=0.07,
            failed_gates_json="[]",
        )
        db.add(dec)
        db.commit()
    finally:
        db.close()

    proc = _run_cli("set-mode", "--application-id", "app_cli_02", "--mode", "shadow")
    assert proc.returncode == 2
    assert b"enforce_to_shadow_forbidden_after_decision_issued" in proc.stderr


def test_cli_set_mode_forbidden_transition_with_force_succeeds():
    db = SessionLocal()
    try:
        _create_application(db, app_id="app_cli_03", founder_id="fnd_cli_3")
        dec = models.Decision(
            id="dec_cli_03",
            application_id="app_cli_03",
            decision="pass",
            policy_version="decision-policy-v1.0.0",
            parameter_set_id="default",
            threshold_required=0.18,
            coherence_observed=0.25,
            margin=0.07,
            failed_gates_json="[]",
        )
        db.add(dec)
        db.commit()
    finally:
        db.close()

    proc = _run_cli(
        "set-mode", "--application-id", "app_cli_03", "--mode", "shadow", "--force"
    )
    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["new_mode"] == SCORING_MODE_SHADOW


def test_cli_set_mode_missing_application_exits_two():
    proc = _run_cli(
        "set-mode", "--application-id", "app_does_not_exist", "--mode", "shadow"
    )
    assert proc.returncode == 2
    assert b"application_not_found" in proc.stderr


def test_cli_set_mode_shadow_to_enforce_round_trip():
    db = SessionLocal()
    try:
        _create_application(
            db,
            app_id="app_cli_04",
            founder_id="fnd_cli_4",
            scoring_mode=SCORING_MODE_SHADOW,
        )
        db.commit()
    finally:
        db.close()

    proc = _run_cli("set-mode", "--application-id", "app_cli_04", "--mode", "enforce")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["previous_mode"] == SCORING_MODE_SHADOW
    assert payload["new_mode"] == SCORING_MODE_ENFORCE
    assert payload["changed"] is True
