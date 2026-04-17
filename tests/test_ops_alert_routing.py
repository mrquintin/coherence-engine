"""Tests for worker ops telemetry alert routing."""

from __future__ import annotations

import json
import urllib.error
from unittest import mock

import pytest

from coherence_engine.server.fund.services import alert_routing
from coherence_engine.server.fund.services.alert_routing import (
    AlertRouterConfig,
    build_alert_envelope,
    drill_route_worker_ops_alert,
    load_alert_router_config,
    route_worker_ops_alert,
    verify_alert_router_config,
)


@pytest.fixture(autouse=True)
def _reset_alert_routing():
    alert_routing.reset_alert_routing_state_for_tests()
    yield
    alert_routing.reset_alert_routing_state_for_tests()


def test_load_config_defaults(monkeypatch):
    monkeypatch.delenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", raising=False)
    cfg = load_alert_router_config()
    assert cfg.mode == "none"


def test_route_skips_when_no_warn_tags(tmp_path, monkeypatch):
    out = tmp_path / "alerts.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(out))
    route_worker_ops_alert([], {"component": "scoring", "warn": []})
    assert not out.exists()


def test_file_mode_appends_envelope(tmp_path, monkeypatch):
    out = tmp_path / "alerts.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(out))
    payload = {"component": "scoring", "warn": ["queue_depth"]}
    route_worker_ops_alert(["queue_depth"], payload)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["schema"] == "coherence_fund_worker_ops_alert/v1"
    assert row["warn_tags"] == ["queue_depth"]
    assert row["severities"]["queue_depth"] == "warning"
    assert row["payload"]["component"] == "scoring"


def test_cooldown_suppresses_repeat(tmp_path, monkeypatch):
    out = tmp_path / "alerts.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(out))
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_COOLDOWN_SECONDS", "3600")
    payload = {"component": "outbox", "warn": ["queue_depth"]}
    route_worker_ops_alert(["queue_depth"], payload)
    route_worker_ops_alert(["queue_depth"], payload)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_severity_map_from_env(tmp_path, monkeypatch):
    out = tmp_path / "a.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(out))
    monkeypatch.setenv(
        "COHERENCE_FUND_OPS_ALERT_SEVERITY_MAP",
        json.dumps({"queue_depth": "critical"}),
    )
    payload = {"component": "scoring", "warn": ["queue_depth"]}
    route_worker_ops_alert(["queue_depth"], payload)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["severities"]["queue_depth"] == "critical"


def test_dedupe_component_merges_tags(tmp_path, monkeypatch):
    out = tmp_path / "a.jsonl"
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(out))
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_DEDUPE_KEY", "component")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_COOLDOWN_SECONDS", "3600")
    route_worker_ops_alert(["queue_depth"], {"component": "x", "warn": ["queue_depth"]})
    route_worker_ops_alert(["failed_dlq"], {"component": "x", "warn": ["failed_dlq"]})
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_webhook_posts_json(monkeypatch):
    cfg = AlertRouterConfig(
        mode="webhook",
        webhook_url="https://example.invalid/hook",
        webhook_token="secret",
        webhook_timeout_seconds=2.0,
        cooldown_seconds=0,
    )
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        captured["data"] = req.data
        captured["auth"] = req.headers.get("Authorization", "")
        mock_resp = mock.Mock()
        mock_resp.read.return_value = b"{}"
        cm = mock.Mock()
        cm.__enter__.return_value = mock_resp
        cm.__exit__.return_value = False
        return cm

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        route_worker_ops_alert(["queue_depth"], {"component": "scoring"}, cfg=cfg)

    assert captured["timeout"] == 2.0
    assert captured["auth"] == "Bearer secret"
    body = json.loads(captured["data"].decode("utf-8"))
    assert body["warn_tags"] == ["queue_depth"]


def test_routing_errors_swallowed(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "file")
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_FILE_PATH", str(tmp_path / "nope" / "a.jsonl"))
    with mock.patch.object(alert_routing, "_append_alert_jsonl", side_effect=OSError("fail")):
        route_worker_ops_alert(["queue_depth"], {"component": "scoring"})


def test_build_alert_envelope_includes_dedupe():
    cfg = AlertRouterConfig(dedupe_key="component_tags")
    env = build_alert_envelope(
        ["oldest_latency", "queue_depth"],
        {"component": "outbox"},
        cfg,
        fired_at_unix=123.0,
    )
    assert env["dedupe_key"] == "outbox:oldest_latency|queue_depth"
    assert env["fired_at_unix"] == 123.0


def test_pagerduty_posts_events_v2(monkeypatch):
    cfg = AlertRouterConfig(
        mode="pagerduty",
        pagerduty_routing_key="rkey",
        pagerduty_events_url="https://events.example.invalid/v2/enqueue",
        webhook_timeout_seconds=2.0,
        cooldown_seconds=0,
    )
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        mock_resp = mock.Mock()
        mock_resp.read.return_value = b"{}"
        cm = mock.Mock()
        cm.__enter__.return_value = mock_resp
        cm.__exit__.return_value = False
        return cm

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        route_worker_ops_alert(["queue_depth"], {"component": "scoring"}, cfg=cfg)

    assert captured["body"]["routing_key"] == "rkey"
    assert captured["body"]["event_action"] == "trigger"
    assert "summary" in captured["body"]["payload"]


def test_pagerduty_falls_back_to_webhook_without_routing_key(monkeypatch):
    cfg = AlertRouterConfig(
        mode="pagerduty",
        pagerduty_routing_key="",
        webhook_url="https://hook.example.invalid/x",
        webhook_token="t",
        webhook_timeout_seconds=2.0,
        cooldown_seconds=0,
    )
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization", "")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        mock_resp = mock.Mock()
        mock_resp.read.return_value = b"ok"
        cm = mock.Mock()
        cm.__enter__.return_value = mock_resp
        cm.__exit__.return_value = False
        return cm

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        route_worker_ops_alert(["failed_dlq"], {"component": "outbox"}, cfg=cfg)

    assert captured["body"]["schema"] == "coherence_fund_worker_ops_alert/v1"
    assert captured["auth"] == "Bearer t"


def test_opsgenie_posts_with_genie_key(monkeypatch):
    cfg = AlertRouterConfig(
        mode="opsgenie",
        opsgenie_api_key="abc",
        opsgenie_api_url="https://api.example.invalid/v2/alerts",
        webhook_timeout_seconds=2.0,
        cooldown_seconds=0,
    )
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization", "")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        mock_resp = mock.Mock()
        mock_resp.read.return_value = b"{}"
        cm = mock.Mock()
        cm.__enter__.return_value = mock_resp
        cm.__exit__.return_value = False
        return cm

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        route_worker_ops_alert(["queue_depth"], {"component": "scoring"}, cfg=cfg)

    assert captured["auth"] == "GenieKey abc"
    assert captured["body"]["message"]
    assert captured["body"]["priority"] == "P3"


def test_alertmanager_webhook_payload_shape(monkeypatch):
    cfg = AlertRouterConfig(
        mode="alertmanager",
        alertmanager_webhook_url="https://am.example.invalid/hook",
        webhook_timeout_seconds=2.0,
        cooldown_seconds=0,
    )
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        mock_resp = mock.Mock()
        mock_resp.read.return_value = b"{}"
        cm = mock.Mock()
        cm.__enter__.return_value = mock_resp
        cm.__exit__.return_value = False
        return cm

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        route_worker_ops_alert(["oldest_latency"], {"component": "outbox"}, cfg=cfg)

    assert captured["body"]["version"] == "4"
    assert captured["body"]["status"] == "firing"
    assert len(captured["body"]["alerts"]) == 1
    assert captured["body"]["alerts"][0]["labels"]["alertname"] == "CoherenceFundWorkerOps"


def test_drill_surfaces_delivery_failure(monkeypatch):
    cfg = AlertRouterConfig(
        mode="webhook",
        webhook_url="https://example.invalid/hook",
        cooldown_seconds=0,
    )

    def boom(*_a, **_kw):
        raise urllib.error.URLError("nope")

    with mock.patch("urllib.request.urlopen", side_effect=boom):
        res = drill_route_worker_ops_alert(cfg=cfg)

    assert res.ok is False
    assert "nope" in res.detail


def test_drill_does_not_record_cooldown(tmp_path, monkeypatch):
    out = tmp_path / "a.jsonl"
    cfg = AlertRouterConfig(mode="file", file_path=str(out), cooldown_seconds=3600)
    drill_route_worker_ops_alert(cfg=cfg)
    route_worker_ops_alert(["synthetic_drill"], {"component": "ops_drill"}, cfg=cfg)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_verify_flags_missing_webhook_url(monkeypatch):
    monkeypatch.setenv("COHERENCE_FUND_OPS_ALERT_ROUTER_MODE", "webhook")
    monkeypatch.delenv("COHERENCE_FUND_OPS_ALERT_WEBHOOK_URL", raising=False)
    issues = verify_alert_router_config()
    assert any("WEBHOOK_URL" in i for i in issues)


def test_worker_bypass_cooldown_kwarg(tmp_path, monkeypatch):
    out = tmp_path / "a.jsonl"
    cfg = AlertRouterConfig(
        mode="file",
        file_path=str(out),
        cooldown_seconds=3600,
    )
    route_worker_ops_alert(["queue_depth"], {"component": "x"}, cfg=cfg)
    route_worker_ops_alert(["queue_depth"], {"component": "x"}, cfg=cfg, bypass_cooldown=True)
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2
