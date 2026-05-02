"""Workflow orchestrator checkpoint + resume tests (prompt 15).

Verifies:

* Happy-path run produces exactly one ``WorkflowRun`` and one
  ``WorkflowStep`` per declared stage, all in ``succeeded`` status.
* Injecting a raise in a mid-pipeline stage leaves earlier stages
  in ``succeeded`` and the failing stage in ``failed``; a resume
  picks up from the failing stage and completes the run.
* Resume with a tampered upstream input (simulated by mutating
  the application row between runs) refuses without ``--force`` and
  proceeds with ``force=True``.
* Stage execution order matches ``STEPS`` declaration order.
* Event emission order is preserved (InterviewCompleted ->
  ArgumentCompiled -> CoherenceScored -> DecisionIssued).
"""

from __future__ import annotations

import os

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import workflow as workflow_mod
from coherence_engine.server.fund.services.notification_backends import (
    DryRunBackend,
)
from coherence_engine.server.fund.services.workflow import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STEP_NAMES,
    STEPS,
    WorkflowError,
    WorkflowResumeRefused,
    compute_digest,
    run_workflow,
)


# ---------------------------------------------------------------------------
# Fixture: clean schema for each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BOILERPLATE_TRANSCRIPT = (
    "We reduce back-office processing time for small businesses. "
    "Our software integrates accounting, invoicing, and procurement. "
    "Pilot users reported fewer reconciliation errors and faster closes. "
    "The market has millions of SMBs with fragmented workflows. "
    "We sell a subscription model with expansion to payments. "
    "Founders have 10 years of combined operating experience. "
    "Revenue is growing month over month with 15 paying pilots."
)


def _seed_application(
    db,
    *,
    app_id: str,
    founder_id: str,
    scoring_mode: str = "enforce",
    transcript_text: str = _BOILERPLATE_TRANSCRIPT,
    compliance_status: str = "clear",
) -> None:
    founder = models.Founder(
        id=founder_id,
        full_name="Workflow Tester",
        email=f"{founder_id}@example.com",
        company_name="Workflow Co",
        country="US",
    )
    app = models.Application(
        id=app_id,
        founder_id=founder_id,
        one_liner="Workflow automation for SMB finance ops.",
        requested_check_usd=75_000,
        use_of_funds_summary="hire + pilot",
        preferred_channel="web_voice",
        transcript_text=transcript_text,
        domain_primary="market_economics",
        compliance_status=compliance_status,
        status="scoring_queued",
        scoring_mode=scoring_mode,
    )
    db.add_all([founder, app])
    db.flush()


def _ordered_step_names(db, run_id: str) -> list[str]:
    rows = (
        db.query(models.WorkflowStep)
        .filter(models.WorkflowStep.workflow_run_id == run_id)
        .order_by(models.WorkflowStep.created_at.asc())
        .all()
    )
    return [row.name for row in rows]


# ---------------------------------------------------------------------------
# Static invariants
# ---------------------------------------------------------------------------


def test_steps_table_matches_required_stage_order():
    assert STEP_NAMES == (
        "intake",
        "transcript_quality",
        "compile",
        "ontology",
        "domain_mix",
        "score",
        "decide",
        "artifact",
        "notify",
    )


def test_steps_has_nine_stages():
    assert len(STEPS) == 9


def test_compute_digest_is_stable_across_key_order():
    a = compute_digest({"x": 1, "y": [1, 2]})
    b = compute_digest({"y": [1, 2], "x": 1})
    assert a == b


def test_compute_digest_changes_when_value_changes():
    a = compute_digest({"transcript_sha256": "abc"})
    b = compute_digest({"transcript_sha256": "def"})
    assert a != b


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_produces_single_run_and_all_succeeded_steps(tmp_path):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_happy", founder_id="fnd_happy"
        )
        db.commit()

        run = run_workflow(
            db,
            "app_happy",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()

        assert run.status == STATUS_SUCCEEDED
        assert run.current_step == "notify"
        assert run.finished_at is not None
        assert run.error == ""

        runs = db.query(models.WorkflowRun).all()
        assert len(runs) == 1

        steps = db.query(models.WorkflowStep).all()
        assert len(steps) == 9
        assert all(s.status == STATUS_SUCCEEDED for s in steps)
        assert all(s.input_digest for s in steps)
        assert all(s.output_digest for s in steps)
        assert _ordered_step_names(db, run.id) == list(STEP_NAMES)
    finally:
        db.close()


def test_happy_path_emits_canonical_events_in_order(tmp_path):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_events", founder_id="fnd_events"
        )
        db.commit()

        run_workflow(
            db,
            "app_events",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()

        events = (
            db.query(models.EventOutbox)
            .filter(models.EventOutbox.event_type.in_(
                [
                    "InterviewCompleted",
                    "ArgumentCompiled",
                    "CoherenceScored",
                    "DecisionIssued",
                ]
            ))
            .order_by(models.EventOutbox.occurred_at.asc())
            .all()
        )
        assert [e.event_type for e in events] == [
            "InterviewCompleted",
            "ArgumentCompiled",
            "CoherenceScored",
            "DecisionIssued",
        ]
    finally:
        db.close()


def test_happy_path_writes_decision_row(tmp_path):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_dec", founder_id="fnd_dec"
        )
        db.commit()

        run_workflow(
            db,
            "app_dec",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()

        decision = db.query(models.Decision).one()
        assert decision.application_id == "app_dec"
        assert decision.decision in {"pass", "fail", "manual_review"}
    finally:
        db.close()


def test_happy_path_dispatches_notification(tmp_path):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_ntf", founder_id="fnd_ntf"
        )
        db.commit()

        run_workflow(
            db,
            "app_ntf",
            notification_backend=DryRunBackend(tmp_path),
        )
        db.commit()

        logs = db.query(models.NotificationLog).all()
        assert len(logs) == 1
        assert logs[0].status == "sent"
    finally:
        db.close()


def test_shadow_mode_notify_stage_skips_dispatch_but_succeeds(tmp_path):
    db = SessionLocal()
    try:
        _seed_application(
            db,
            app_id="app_shadow_wf",
            founder_id="fnd_shadow_wf",
            scoring_mode="shadow",
        )
        db.commit()

        run = run_workflow(
            db,
            "app_shadow_wf",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()

        assert run.status == STATUS_SUCCEEDED
        logs = db.query(models.NotificationLog).all()
        assert len(logs) == 0  # shadow mode suppresses founder dispatch

        notify_step = (
            db.query(models.WorkflowStep)
            .filter(models.WorkflowStep.name == "notify")
            .one()
        )
        assert notify_step.status == STATUS_SUCCEEDED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_run_workflow_requires_application_id():
    db = SessionLocal()
    try:
        with pytest.raises(WorkflowError):
            run_workflow(db, "")
    finally:
        db.close()


def test_run_workflow_raises_on_missing_application():
    db = SessionLocal()
    try:
        with pytest.raises(WorkflowError) as excinfo:
            run_workflow(db, "app_does_not_exist")
        assert "application_not_found" in str(excinfo.value)
        # The orchestrator marks the WorkflowRun row as ``failed``
        # before re-raising; caller owns the commit decision. Commit
        # here so we can observe the persisted checkpoint state.
        db.commit()
        run = db.query(models.WorkflowRun).one()
        assert run.status == STATUS_FAILED
        assert "application_not_found" in run.error
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Failure + resume
# ---------------------------------------------------------------------------


def test_injected_failure_in_ontology_leaves_earlier_steps_succeeded(
    tmp_path, monkeypatch
):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_fail", founder_id="fnd_fail"
        )
        db.commit()

        real_ontology = workflow_mod._stage_ontology

        def _boom(ctx):
            raise RuntimeError("injected_ontology_failure")

        monkeypatch.setattr(workflow_mod, "_stage_ontology", _boom)
        # Also patch the STEPS tuple entry (compiled at import time).
        patched_steps = [
            (name, _boom if name == "ontology" else fn)
            for name, fn in workflow_mod.STEPS
        ]
        monkeypatch.setattr(workflow_mod, "STEPS", patched_steps)

        with pytest.raises(RuntimeError, match="injected_ontology_failure"):
            run_workflow(
                db,
                "app_fail",
                notification_dry_run_dir=tmp_path,
            )
        db.commit()

        steps = {
            s.name: s
            for s in db.query(models.WorkflowStep).all()
        }
        assert steps["intake"].status == STATUS_SUCCEEDED
        assert steps["transcript_quality"].status == STATUS_SUCCEEDED
        assert steps["compile"].status == STATUS_SUCCEEDED
        assert steps["ontology"].status == STATUS_FAILED
        assert "injected_ontology_failure" in steps["ontology"].error
        # Stages strictly after ontology must not have been executed.
        for later in ("domain_mix", "score", "decide", "artifact", "notify"):
            assert later not in steps

        run = db.query(models.WorkflowRun).one()
        assert run.status == STATUS_FAILED
        assert run.current_step == "ontology"
        assert "ontology" in run.error

        # Restore the real stage fn and resume.
        monkeypatch.setattr(workflow_mod, "_stage_ontology", real_ontology)
        restored_steps = [
            (name, real_ontology if name == "ontology" else fn)
            for name, fn in workflow_mod.STEPS
        ]
        monkeypatch.setattr(workflow_mod, "STEPS", restored_steps)

        resumed = run_workflow(
            db,
            "app_fail",
            resume=True,
            notification_dry_run_dir=tmp_path,
        )
        db.commit()

        assert resumed.id == run.id, "resume must reuse the existing run row"
        assert resumed.status == STATUS_SUCCEEDED
        assert resumed.error == ""
        final_steps = {
            s.name: s
            for s in db.query(models.WorkflowStep).all()
        }
        assert all(
            final_steps[name].status == STATUS_SUCCEEDED
            for name in STEP_NAMES
        )
    finally:
        db.close()


def test_resume_with_all_succeeded_steps_is_noop_but_completes(tmp_path):
    """A resume against a fully-succeeded run becomes an idempotent
    fast-forward: no steps re-execute (digests match), the run row
    flips to ``succeeded``, and no duplicate step rows are created.
    """
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_noop", founder_id="fnd_noop"
        )
        db.commit()
        run1 = run_workflow(
            db, "app_noop", notification_dry_run_dir=tmp_path
        )
        db.commit()
        n_steps_before = db.query(models.WorkflowStep).count()

        run2 = run_workflow(
            db,
            "app_noop",
            resume=True,
            notification_dry_run_dir=tmp_path,
        )
        db.commit()
        assert run2.id == run1.id
        assert run2.status == STATUS_SUCCEEDED
        assert db.query(models.WorkflowStep).count() == n_steps_before
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tampered-input resume refusal
# ---------------------------------------------------------------------------


def test_resume_refuses_when_upstream_input_digest_drifts(
    tmp_path, monkeypatch
):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_tamper", founder_id="fnd_tamper"
        )
        db.commit()

        # Force a failure at the decide stage so upstream steps
        # (including compile) are persisted as succeeded.
        def _boom(ctx):
            raise RuntimeError("stop_before_tamper")

        patched_steps = [
            (name, _boom if name == "decide" else fn)
            for name, fn in workflow_mod.STEPS
        ]
        monkeypatch.setattr(workflow_mod, "STEPS", patched_steps)

        with pytest.raises(RuntimeError, match="stop_before_tamper"):
            run_workflow(
                db,
                "app_tamper",
                notification_dry_run_dir=tmp_path,
            )
        db.commit()

        # Restore the real STEPS for resume.
        monkeypatch.undo()

        # Tamper with upstream input: mutate the application's
        # transcript so the compile stage's input_digest changes.
        app_row = (
            db.query(models.Application)
            .filter(models.Application.id == "app_tamper")
            .one()
        )
        app_row.transcript_text = (
            _BOILERPLATE_TRANSCRIPT + " AN ENTIRELY NEW SENTENCE APPENDED."
        )
        db.commit()

        # Resume WITHOUT --force should refuse.
        with pytest.raises(WorkflowResumeRefused) as excinfo:
            run_workflow(
                db,
                "app_tamper",
                resume=True,
                notification_dry_run_dir=tmp_path,
            )
        db.rollback()
        assert "input_digest_mismatch" in str(excinfo.value)
    finally:
        db.close()


def test_resume_with_force_bypasses_input_digest_drift_check(
    tmp_path, monkeypatch
):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_force", founder_id="fnd_force"
        )
        db.commit()

        def _boom(ctx):
            raise RuntimeError("stop_before_tamper")

        patched_steps = [
            (name, _boom if name == "decide" else fn)
            for name, fn in workflow_mod.STEPS
        ]
        monkeypatch.setattr(workflow_mod, "STEPS", patched_steps)

        with pytest.raises(RuntimeError):
            run_workflow(
                db,
                "app_force",
                notification_dry_run_dir=tmp_path,
            )
        db.commit()

        monkeypatch.undo()

        app_row = (
            db.query(models.Application)
            .filter(models.Application.id == "app_force")
            .one()
        )
        app_row.transcript_text = (
            _BOILERPLATE_TRANSCRIPT + " ADDITIONAL UPSTREAM MUTATION."
        )
        db.commit()

        resumed = run_workflow(
            db,
            "app_force",
            resume=True,
            force=True,
            notification_dry_run_dir=tmp_path,
        )
        db.commit()
        assert resumed.status == STATUS_SUCCEEDED
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Application-service integration
# ---------------------------------------------------------------------------


def test_application_service_delegates_to_workflow(tmp_path):
    from coherence_engine.server.fund.repositories.application_repository import (
        ApplicationRepository,
    )
    from coherence_engine.server.fund.services.application_service import (
        ApplicationService,
    )
    from coherence_engine.server.fund.services.event_publisher import (
        EventPublisher,
    )

    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_svc", founder_id="fnd_svc"
        )
        db.commit()
        svc = ApplicationService(
            ApplicationRepository(db),
            EventPublisher(db, strict_events=False),
            notification_dry_run_dir=tmp_path,
        )
        run = svc.run_application_workflow("app_svc")
        db.commit()
        assert run.status == STATUS_SUCCEEDED
        assert db.query(models.WorkflowRun).count() == 1
        assert db.query(models.WorkflowStep).count() == 9
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CLI subcommand smoke
# ---------------------------------------------------------------------------


def test_cli_workflow_parser_is_registered():
    """The argparse parser must expose ``workflow run`` and
    ``workflow resume`` subcommands — regression guard so a future
    refactor does not silently drop them."""
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [_sys.executable, "-m", "coherence_engine", "workflow", "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
        timeout=30,
    )
    assert result.returncode == 0
    assert "run" in result.stdout
    assert "resume" in result.stdout
