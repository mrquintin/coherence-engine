"""Tests for governance escalation routing proof and resolution helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTE_SCRIPT = REPO_ROOT / "deploy" / "scripts" / "route_governance_attestation_escalation.py"
EXAMPLE_MAP = REPO_ROOT / "deploy" / "ops" / "governance-attestation-escalation-routing-map.example.json"


def _load_route_module():
    spec = importlib.util.spec_from_file_location("route_governance_attestation_escalation", ROUTE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_route(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROUTE_SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


def test_example_routing_map_exists_and_has_canary_prod_channels() -> None:
    data = json.loads(EXAMPLE_MAP.read_text(encoding="utf-8"))
    assert data.get("schema_version") == 1
    by_gh = data["by_github_environment"]
    assert by_gh["uncertainty-governance-canary"]["escalation_channel"] == "governance_canary"
    assert by_gh["uncertainty-governance-prod"]["escalation_channel"] == "governance_prod"
    assert "GOVERNANCE_ESCALATION_WEBHOOK_URL_CANARY" in json.dumps(by_gh)
    assert "GOVERNANCE_ESCALATION_WEBHOOK_URL_PROD" in json.dumps(by_gh)


def test_build_routing_resolution_matches_example_map() -> None:
    mod = _load_route_module()
    mapping = mod._load_routing_map(EXAMPLE_MAP)
    r = mod.build_routing_resolution(
        mapping,
        github_environment="uncertainty-governance-canary",
        promotion_env="canary",
    )
    assert r["github_environment"] == "uncertainty-governance-canary"
    assert r["promotion_environment"] == "canary"
    assert r["escalation_channel"] == "governance_canary"
    assert r["webhook_environment_variable"] == "GOVERNANCE_ESCALATION_WEBHOOK_URL_CANARY"
    assert r["resolved_file_path"] == "artifacts/governance_escalation_sink_canary.ndjson"
    assert r["webhook_url_configured"] is False


def test_emit_routing_proof_cli_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "proof.json"
    proc = _run_route(
        "emit-routing-proof",
        "--map-json",
        str(EXAMPLE_MAP),
        "--routing-proof-out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["record_type"] == "governance_escalation_routing_proof"
    assert data["schema_version"] == 1
    assert len(data["channels"]) == 2
    labels = {(c["github_environment"], c["promotion_environment"]) for c in data["channels"]}
    assert ("uncertainty-governance-canary", "canary") in labels
    assert ("uncertainty-governance-prod", "prod") in labels


def test_emit_routing_proof_requires_map(tmp_path: Path) -> None:
    proc = _run_route(
        "emit-routing-proof",
        "--routing-proof-out",
        str(tmp_path / "p.json"),
        env={"GOVERNANCE_ESCALATION_ROUTING_MAP_JSON": ""},
    )
    assert proc.returncode == 1
    assert "emit-routing-proof requires" in proc.stderr


def test_emit_webhook_delivery_receipt_cli_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "receipt.json"
    proc = _run_route(
        "emit-webhook-delivery-receipt",
        "--map-json",
        str(EXAMPLE_MAP),
        "--webhook-receipt-out",
        str(out),
        env={
            "GOVERNANCE_ESCALATION_RECEIPT_REFERENCE_TIME_UTC": "2026-01-15T12:00:00Z",
            "GITHUB_RUN_ID": "42",
            "GITHUB_REPOSITORY": "org/coherence_engine",
        },
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["record_type"] == "governance_escalation_webhook_delivery_receipt"
    assert data["schema_version"] == 1
    assert len(data["channels"]) == 2
    for ch in data["channels"]:
        assert "canonical_payload_sha256" in ch
        assert ch["delivery"]["mode"] in ("dry_run", "skipped_no_url", "live_post")


def test_emit_webhook_delivery_receipt_requires_map(tmp_path: Path) -> None:
    proc = _run_route(
        "emit-webhook-delivery-receipt",
        "--webhook-receipt-out",
        str(tmp_path / "r.json"),
        env={"GOVERNANCE_ESCALATION_ROUTING_MAP_JSON": ""},
    )
    assert proc.returncode == 1
    assert "emit-webhook-delivery-receipt requires" in proc.stderr


def test_webhook_url_configured_true_when_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_route_module()
    mpath = tmp_path / "map.json"
    mpath.write_text(
        json.dumps(
            {
                "by_promotion_environment": {
                    "prod": {"webhook_environment_variable": "TEST_GOV_HOOK"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_GOV_HOOK", "https://example.invalid/x")
    mapping = mod._load_routing_map(mpath)
    assert mod.build_routing_resolution(
        mapping,
        github_environment="",
        promotion_env="prod",
    )["webhook_url_configured"] is True
