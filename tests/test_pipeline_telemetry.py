"""Pipeline stage telemetry tests (prompt 18).

Covers the :func:`record_stage` helper and its integration with the
workflow orchestrator:

* Unit contract of ``record_stage`` — status normalisation, duration
  clamping, counter updates, warn-tagging, optional JSONL + Prometheus
  textfile sinks.
* End-to-end pipeline telemetry — a happy-path workflow emits exactly
  one ``status=success`` event per declared stage; a forced failure
  mid-pipeline emits ``success`` for preceding stages and one
  ``failure`` for the failing stage with no events for downstream
  stages.
* JSONL sink captures one line per stage per run when configured.
* Latency values are non-negative.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services import ops_telemetry
from coherence_engine.server.fund.services import workflow as workflow_mod
from coherence_engine.server.fund.services.ops_telemetry import (
    PIPELINE_STAGE_EVENT_MARKER,
    get_pipeline_stage_counters_snapshot,
    record_stage,
    reset_pipeline_stage_counters,
)
from coherence_engine.server.fund.services.workflow import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STEP_NAMES,
    run_workflow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_pipeline_counters() -> None:
    """Clear the in-process counter state before every test case."""
    reset_pipeline_stage_counters()
    yield
    reset_pipeline_stage_counters()


@pytest.fixture(autouse=True)
def _scrub_pipeline_env(monkeypatch) -> None:
    """Ensure the optional sinks start un-configured in each test.

    Tests that want the JSONL or Prometheus textfile sink configure
    the env var explicitly with ``monkeypatch.setenv``.
    """
    for var in (
        "COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH",
        "COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH",
        "COHERENCE_FUND_PIPELINE_STAGE_DURATION_WARN_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
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
) -> None:
    founder = models.Founder(
        id=founder_id,
        full_name="Pipeline Telemetry Tester",
        email=f"{founder_id}@example.com",
        company_name="Telemetry Co",
        country="US",
    )
    app = models.Application(
        id=app_id,
        founder_id=founder_id,
        one_liner="Telemetry validation for pipeline stage observability.",
        requested_check_usd=50_000,
        use_of_funds_summary="telemetry rollout",
        preferred_channel="web_voice",
        transcript_text=_BOILERPLATE_TRANSCRIPT,
        domain_primary="market_economics",
        compliance_status="clear",
        status="scoring_queued",
        scoring_mode=scoring_mode,
    )
    db.add_all([founder, app])
    db.flush()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# record_stage unit contract
# ---------------------------------------------------------------------------


def test_record_stage_emits_payload_with_normalised_fields():
    payload = record_stage("intake", 0.125, "success")
    assert payload["marker"] == PIPELINE_STAGE_EVENT_MARKER
    assert payload["stage"] == "intake"
    assert payload["status"] == "success"
    assert payload["duration_s"] == pytest.approx(0.125)
    assert payload["warn"] == []


def test_record_stage_clamps_negative_duration_to_zero():
    payload = record_stage("intake", -42.0, "success")
    assert payload["duration_s"] == 0.0
    assert payload["duration_s_raw"] == -42.0
    snapshot = get_pipeline_stage_counters_snapshot()
    assert snapshot["intake"]["last_duration_s"] == 0.0


def test_record_stage_updates_counters_for_success_and_failure():
    record_stage("intake", 0.1, "success")
    record_stage("intake", 0.1, "success")
    record_stage("intake", 0.1, "failure")
    record_stage("transcript_quality", 0.2, "success")

    snapshot = get_pipeline_stage_counters_snapshot()
    assert snapshot["intake"]["events"] == {"success": 2, "failure": 1}
    assert snapshot["transcript_quality"]["events"] == {"success": 1}
    assert snapshot["intake"]["total_duration_s"] == pytest.approx(0.3)


def test_record_stage_warn_tag_fires_when_threshold_exceeded(monkeypatch):
    monkeypatch.setenv(
        "COHERENCE_FUND_PIPELINE_STAGE_DURATION_WARN_SECONDS", "1.0"
    )
    payload_fast = record_stage("ontology", 0.5, "success")
    payload_slow = record_stage("ontology", 2.5, "success")

    assert payload_fast["warn"] == []
    assert "duration_budget" in payload_slow["warn"]

    snapshot = get_pipeline_stage_counters_snapshot()
    assert snapshot["ontology"]["warn_count"] == 1


def test_record_stage_appends_one_jsonl_line_per_call(tmp_path, monkeypatch):
    sink = tmp_path / "pipeline.jsonl"
    monkeypatch.setenv(
        "COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH", str(sink)
    )

    record_stage("intake", 0.1, "success")
    record_stage("transcript_quality", 0.2, "success")
    record_stage("ontology", 0.05, "failure", extra={"error_type": "Boom"})

    events = _read_jsonl(sink)
    assert len(events) == 3
    stages = [e["stage"] for e in events]
    assert stages == ["intake", "transcript_quality", "ontology"]
    assert events[-1]["extra"]["error_type"] == "Boom"


def test_record_stage_writes_prometheus_textfile_when_configured(
    tmp_path, monkeypatch
):
    prom = tmp_path / "pipeline.prom"
    monkeypatch.setenv(
        "COHERENCE_FUND_PIPELINE_TELEMETRY_PROMETHEUS_TEXTFILE_PATH", str(prom)
    )
    record_stage("intake", 0.1, "success")
    record_stage("intake", 0.2, "failure")
    record_stage("ontology", 0.3, "success")

    body = prom.read_text(encoding="utf-8")
    assert "coherence_fund_pipeline_stage_events_total" in body
    assert 'stage="intake"' in body
    assert 'status="success"' in body
    assert 'status="failure"' in body
    assert "coherence_fund_pipeline_stage_last_duration_seconds" in body
    assert "coherence_fund_pipeline_stage_duration_seconds_sum" in body


def test_record_stage_logs_warning_on_failure_status(caplog):
    with caplog.at_level("INFO", logger="coherence_engine.fund.pipeline_telemetry"):
        record_stage("ontology", 0.1, "failure")
    failure_records = [
        r for r in caplog.records if PIPELINE_STAGE_EVENT_MARKER in r.getMessage()
    ]
    assert any(r.levelname == "WARNING" for r in failure_records)


# ---------------------------------------------------------------------------
# Workflow orchestrator integration — forced-failure fan-out
# ---------------------------------------------------------------------------


def test_forced_failure_emits_one_failure_event_and_success_for_prior_stages(
    tmp_path, monkeypatch, _reset_fund_tables
):
    """A forced mid-pipeline failure emits exactly one ``status=failure``
    telemetry event for the failing stage and ``status=success`` for each
    preceding stage; downstream stages emit no events."""
    db = SessionLocal()
    try:
        _seed_application(db, app_id="app_tel_fail", founder_id="fnd_tel_fail")
        db.commit()

        def _boom(ctx):
            raise RuntimeError("injected_ontology_failure")

        patched_steps = [
            (name, _boom if name == "ontology" else fn)
            for name, fn in workflow_mod.STEPS
        ]
        monkeypatch.setattr(workflow_mod, "STEPS", patched_steps)

        with pytest.raises(RuntimeError, match="injected_ontology_failure"):
            run_workflow(
                db,
                "app_tel_fail",
                notification_dry_run_dir=tmp_path,
            )
        db.commit()
    finally:
        db.close()

    snapshot = get_pipeline_stage_counters_snapshot()

    # Preceding stages: one success each, no failures.
    for prior in ("intake", "transcript_quality", "compile"):
        assert prior in snapshot, f"{prior} must emit telemetry"
        assert snapshot[prior]["events"].get("success", 0) == 1
        assert snapshot[prior]["events"].get("failure", 0) == 0

    # Failing stage: exactly one failure event, zero successes.
    assert snapshot["ontology"]["events"].get("failure", 0) == 1
    assert snapshot["ontology"]["events"].get("success", 0) == 0

    # Downstream stages must be absent from the counter snapshot.
    for downstream in ("domain_mix", "score", "decide", "artifact", "notify"):
        assert downstream not in snapshot, (
            f"{downstream} must not emit telemetry after upstream failure"
        )

    # Every observed duration is non-negative.
    for stage_data in snapshot.values():
        assert stage_data["last_duration_s"] >= 0.0
        assert stage_data["total_duration_s"] >= 0.0


def test_happy_path_emits_one_success_per_stage(
    tmp_path, _reset_fund_tables
):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_tel_ok", founder_id="fnd_tel_ok"
        )
        db.commit()
        run = run_workflow(
            db,
            "app_tel_ok",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()
        assert run.status == STATUS_SUCCEEDED
    finally:
        db.close()

    snapshot = get_pipeline_stage_counters_snapshot()
    # Every declared stage produced exactly one success event, no
    # failures, non-negative duration.
    for stage in STEP_NAMES:
        assert stage in snapshot, f"{stage} must emit telemetry"
        events = snapshot[stage]["events"]
        assert events.get("success", 0) == 1, (
            f"{stage} should have one success event, got {events}"
        )
        assert events.get("failure", 0) == 0, (
            f"{stage} should have zero failure events, got {events}"
        )
        assert snapshot[stage]["last_duration_s"] >= 0.0


def test_jsonl_sink_receives_one_line_per_stage_per_run(
    tmp_path, monkeypatch, _reset_fund_tables
):
    sink = tmp_path / "pipeline.jsonl"
    monkeypatch.setenv(
        "COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH", str(sink)
    )

    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_tel_jsonl", founder_id="fnd_tel_jsonl"
        )
        db.commit()
        run = run_workflow(
            db,
            "app_tel_jsonl",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()
        assert run.status == STATUS_SUCCEEDED
    finally:
        db.close()

    assert sink.exists(), "JSONL sink file must be created"
    events = _read_jsonl(sink)
    # One line per stage per run.
    assert len(events) == len(STEP_NAMES)
    assert [e["stage"] for e in events] == list(STEP_NAMES)
    assert all(e["status"] == "success" for e in events)
    assert all(e["duration_s"] >= 0.0 for e in events)
    # Every record carries the application_id + workflow ids for
    # log correlation.
    for evt in events:
        assert evt["extra"]["application_id"] == "app_tel_jsonl"
        assert evt["extra"]["workflow_run_id"]
        assert evt["extra"]["workflow_step_id"]


def test_jsonl_sink_records_failure_event_on_forced_failure(
    tmp_path, monkeypatch, _reset_fund_tables
):
    sink = tmp_path / "pipeline_fail.jsonl"
    monkeypatch.setenv(
        "COHERENCE_FUND_PIPELINE_TELEMETRY_FILE_PATH", str(sink)
    )

    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_tel_jf", founder_id="fnd_tel_jf"
        )
        db.commit()

        def _boom(ctx):
            raise RuntimeError("injected_compile_failure")

        patched_steps = [
            (name, _boom if name == "compile" else fn)
            for name, fn in workflow_mod.STEPS
        ]
        monkeypatch.setattr(workflow_mod, "STEPS", patched_steps)

        with pytest.raises(RuntimeError, match="injected_compile_failure"):
            run_workflow(
                db,
                "app_tel_jf",
                notification_dry_run_dir=tmp_path,
            )
        db.commit()
        run = db.query(models.WorkflowRun).one()
        assert run.status == STATUS_FAILED
    finally:
        db.close()

    events = _read_jsonl(sink)
    # Intake + transcript_quality succeeded before compile failed.
    assert [e["stage"] for e in events] == [
        "intake",
        "transcript_quality",
        "compile",
    ]
    assert [e["status"] for e in events] == ["success", "success", "failure"]
    assert events[-1]["extra"]["error_type"] == "RuntimeError"


def test_telemetry_latency_is_always_non_negative(
    tmp_path, _reset_fund_tables
):
    db = SessionLocal()
    try:
        _seed_application(
            db, app_id="app_tel_lat", founder_id="fnd_tel_lat"
        )
        db.commit()
        run_workflow(
            db,
            "app_tel_lat",
            notification_dry_run_dir=tmp_path,
        )
        db.commit()
    finally:
        db.close()

    snapshot = get_pipeline_stage_counters_snapshot()
    for stage, data in snapshot.items():
        assert data["last_duration_s"] >= 0.0, (
            f"{stage} last_duration_s must be >=0, got {data}"
        )
        assert data["total_duration_s"] >= 0.0, (
            f"{stage} total_duration_s must be >=0, got {data}"
        )


# ---------------------------------------------------------------------------
# Exported surface contract
# ---------------------------------------------------------------------------


def test_ops_telemetry_module_exposes_record_stage():
    assert hasattr(ops_telemetry, "record_stage")
    assert callable(ops_telemetry.record_stage)


def test_workflow_module_imports_record_stage():
    # The orchestrator must reference ``record_stage`` directly so
    # the import wiring is part of the public contract and not
    # accidentally lazy / optional.
    assert "record_stage" in workflow_mod.__dict__, (
        "workflow.py must import record_stage at module scope"
    )
