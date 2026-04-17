"""Tests for centralized ops telemetry sinks (log + optional file + Prometheus textfile)."""

from __future__ import annotations

import json
import logging

from coherence_engine.server.fund.services import alert_routing
from coherence_engine.server.fund.services.ops_telemetry import (
    OPS_ALERT_ROUTE_VERIFICATION_MARKER,
    WORKER_OPS_SNAPSHOT_MARKER,
    emit_worker_ops_snapshot,
    log_worker_ops_alert_route_verification,
    prometheus_text_from_payload,
    verify_worker_ops_alert_routing,
)


def test_emit_worker_ops_snapshot_log_only_default(caplog, monkeypatch):
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH", raising=False)
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH", raising=False)
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_ops_sink")
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "scoring",
        "eligible_queue_depth": 0,
        "oldest_eligible_age_seconds": None,
        "failed_dlq": 0,
        "processing_in_flight": 0,
        "tick": {},
        "warn": [],
    }
    emit_worker_ops_snapshot(log, warn_tags=[], payload=payload)
    assert len(caplog.records) == 1
    assert WORKER_OPS_SNAPSHOT_MARKER in caplog.records[0].message
    assert '"component":"scoring"' in caplog.records[0].message


def test_emit_worker_ops_snapshot_warning_level_when_warn_tags(caplog, monkeypatch):
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH", raising=False)
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH", raising=False)
    caplog.set_level(logging.WARNING)
    log = logging.getLogger("test_ops_sink_warn")
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "outbox",
        "pending_dispatchable": 99,
        "oldest_pending_age_seconds": 10,
        "failed_dlq": 0,
        "tick": {},
        "warn": ["queue_depth"],
    }
    emit_worker_ops_snapshot(log, warn_tags=["queue_depth"], payload=payload)
    assert caplog.records[0].levelno == logging.WARNING


def test_file_sink_appends_jsonl(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH", raising=False)
    out = tmp_path / "ops.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH", str(out))
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_file_sink")
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "scoring",
        "eligible_queue_depth": 2,
        "oldest_eligible_age_seconds": 30,
        "failed_dlq": 0,
        "processing_in_flight": 1,
        "tick": {"processed": 1, "failed": 0, "idle": 0},
        "warn": [],
    }
    emit_worker_ops_snapshot(log, warn_tags=[], payload=payload)
    emit_worker_ops_snapshot(log, warn_tags=[], payload=payload)
    text = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(text) == 2
    assert '"eligible_queue_depth":2' in text[0]


def test_prometheus_textfile_sink_atomic_write(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH", raising=False)
    prom = tmp_path / "worker.prom"
    monkeypatch.setenv("COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH", str(prom))
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test_prom_sink")
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "outbox",
        "pending_dispatchable": 3,
        "oldest_pending_age_seconds": 60,
        "failed_dlq": 1,
        "tick": {"published": 2, "failed": 0, "scanned": 5},
        "warn": [],
    }
    emit_worker_ops_snapshot(log, warn_tags=[], payload=payload)
    body = prom.read_text(encoding="utf-8")
    assert "# HELP coherence_fund_outbox_pending_dispatchable" in body
    assert "coherence_fund_outbox_pending_dispatchable 3" in body
    assert 'coherence_fund_worker_ops_warn_queue_depth{component="outbox"} 0' in body


def test_prometheus_text_from_payload_scoring_warn_labels():
    payload = {
        "component": "scoring",
        "eligible_queue_depth": 10,
        "oldest_eligible_age_seconds": 100,
        "failed_dlq": 0,
        "processing_in_flight": 0,
        "tick": {},
        "warn": ["queue_depth", "oldest_latency"],
    }
    text = prometheus_text_from_payload(payload)
    assert "coherence_fund_scoring_eligible_queue_depth 10" in text
    assert 'coherence_fund_worker_ops_warn_queue_depth{component="scoring"} 1' in text
    assert 'coherence_fund_worker_ops_warn_oldest_latency{component="scoring"} 1' in text
    assert 'coherence_fund_worker_ops_warn_failed_dlq{component="scoring"} 0' in text


def test_prometheus_empty_component_returns_empty():
    assert prometheus_text_from_payload({}) == ""


def test_emit_worker_ops_snapshot_alert_file_on_warn_tags(tmp_path, monkeypatch, caplog):
    alert_routing.reset_alert_routing_state_for_tests()
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_FILE_PATH", raising=False)
    monkeypatch.delenv("COHERENCE_FUND_OPS_TELEMETRY_PROMETHEUS_TEXTFILE_PATH", raising=False)
    alerts = tmp_path / "alerts.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(alerts))
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_COOLDOWN_SECONDS", "0")
    caplog.set_level(logging.WARNING)
    log = logging.getLogger("test_ops_alert_sink")
    payload = {
        "marker": WORKER_OPS_SNAPSHOT_MARKER,
        "component": "scoring",
        "eligible_queue_depth": 9,
        "oldest_eligible_age_seconds": 1,
        "failed_dlq": 0,
        "processing_in_flight": 0,
        "tick": {},
        "warn": ["queue_depth"],
    }
    emit_worker_ops_snapshot(log, warn_tags=["queue_depth"], payload=payload)
    row = json.loads(alerts.read_text(encoding="utf-8").strip())
    assert row["schema"] == "coherence_fund_worker_ops_alert/v1"
    assert row["warn_tags"] == ["queue_depth"]
    assert WORKER_OPS_SNAPSHOT_MARKER in caplog.records[0].message


def test_verify_worker_ops_alert_routing_delegates(monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "none")
    assert verify_worker_ops_alert_routing() == []


def test_log_worker_ops_alert_route_verification_warns_on_issues(caplog, monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "webhook")
    monkeypatch.delenv("COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL", raising=False)
    caplog.set_level(logging.WARNING)
    log = logging.getLogger("test_verify_ops_alert")
    issues = log_worker_ops_alert_route_verification(log)
    assert issues
    assert any(OPS_ALERT_ROUTE_VERIFICATION_MARKER in r.message for r in caplog.records)
