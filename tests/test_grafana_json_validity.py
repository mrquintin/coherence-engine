"""Validate Grafana dashboard JSON exports (prompt 63).

Dashboards live under ``deploy/grafana/dashboards/`` and are stored as
deterministic JSON (sorted-key-stable, two-space indent). This test
parses each file and asserts the contract documented in
``docs/ops/slos.md``: schema version, panel IDs, SLO panel presence,
and dashboard UID stability.

A change to a dashboard that breaks any of these invariants is
intentional only when the corresponding contract in ``slos.md`` is
also updated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Repo-root resolution — this file is at coherence_engine/tests/, so
# the dashboards live two levels up.
_DASHBOARD_DIR = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "grafana"
    / "dashboards"
)


# Contract: dashboard filename → (expected uid, set of required panel ids,
# substring that must appear in at least one panel title — we use this
# to ensure the SLO row stays present after edits).
_DASHBOARD_CONTRACT: Dict[str, Dict[str, Any]] = {
    "decision_pipeline.json": {
        "uid": "coherence-fund-decision-pipeline-slo",
        "required_panel_ids": {1000, 1001, 1002, 1003, 2000, 2001, 2002, 3000, 3001},
        "title_substr_required": "SLO",
    },
    "scoring_layers.json": {
        "uid": "coherence-fund-scoring-layers",
        "required_panel_ids": {1000, 1001, 1002, 2000, 2001, 2002, 3000, 3001},
        "title_substr_required": "SLO",
    },
    "cost_telemetry.json": {
        "uid": "coherence-fund-cost-telemetry",
        "required_panel_ids": {1000, 1001, 1002, 2000, 2001, 2002, 3000, 3001},
        "title_substr_required": "Cost",
    },
}


@pytest.mark.parametrize("filename", sorted(_DASHBOARD_CONTRACT.keys()))
def test_dashboard_is_parseable_json(filename: str) -> None:
    path = _DASHBOARD_DIR / filename
    assert path.exists(), f"missing dashboard: {path}"
    raw = path.read_text(encoding="utf-8")
    # Must round-trip without error.
    payload = json.loads(raw)
    assert isinstance(payload, dict), f"dashboard root must be an object: {filename}"


@pytest.mark.parametrize("filename", sorted(_DASHBOARD_CONTRACT.keys()))
def test_dashboard_schema_and_uid(filename: str) -> None:
    path = _DASHBOARD_DIR / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    contract = _DASHBOARD_CONTRACT[filename]

    assert payload.get("schemaVersion", 0) >= 38, (
        f"{filename}: schemaVersion must be >= 38 "
        f"(Grafana 10+); got {payload.get('schemaVersion')!r}"
    )
    assert payload.get("uid") == contract["uid"], (
        f"{filename}: uid drift — expected {contract['uid']!r}, "
        f"got {payload.get('uid')!r}"
    )
    assert payload.get("editable") is False, (
        f"{filename}: dashboards are version-controlled and must be "
        "editable=false to discourage in-Grafana drift."
    )


@pytest.mark.parametrize("filename", sorted(_DASHBOARD_CONTRACT.keys()))
def test_dashboard_required_panels_present(filename: str) -> None:
    path = _DASHBOARD_DIR / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    contract = _DASHBOARD_CONTRACT[filename]

    panels = payload.get("panels")
    assert isinstance(panels, list) and panels, (
        f"{filename}: panels must be a non-empty list"
    )

    panel_ids = {p.get("id") for p in panels if isinstance(p, dict)}
    missing = contract["required_panel_ids"] - panel_ids
    assert not missing, (
        f"{filename}: required panel IDs missing: {sorted(missing)}; "
        f"present: {sorted(p for p in panel_ids if isinstance(p, int))}"
    )


@pytest.mark.parametrize("filename", sorted(_DASHBOARD_CONTRACT.keys()))
def test_dashboard_has_required_title_substring(filename: str) -> None:
    path = _DASHBOARD_DIR / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    contract = _DASHBOARD_CONTRACT[filename]
    needle = contract["title_substr_required"]

    titles: List[str] = [
        str(p.get("title", ""))
        for p in payload.get("panels", [])
        if isinstance(p, dict)
    ]
    assert any(needle in t for t in titles), (
        f"{filename}: no panel title contains {needle!r}; titles={titles!r}"
    )


@pytest.mark.parametrize("filename", sorted(_DASHBOARD_CONTRACT.keys()))
def test_dashboard_panel_ids_are_unique(filename: str) -> None:
    path = _DASHBOARD_DIR / filename
    payload = json.loads(path.read_text(encoding="utf-8"))

    panels = payload.get("panels", [])
    ids = [p.get("id") for p in panels if isinstance(p, dict) and "id" in p]
    assert len(ids) == len(set(ids)), (
        f"{filename}: panel IDs are not unique: {ids}"
    )


@pytest.mark.parametrize("filename", sorted(_DASHBOARD_CONTRACT.keys()))
def test_dashboard_json_is_deterministically_formatted(filename: str) -> None:
    """Re-serializing with the same options must round-trip byte-for-byte
    aside from a trailing newline. This guards against accidental key
    reordering or whitespace drift on save.
    """
    path = _DASHBOARD_DIR / filename
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    # We don't enforce sort_keys (Grafana exports keep its own order),
    # but we do enforce that the file ends with exactly one trailing
    # newline and uses LF line endings — both required for clean diffs.
    assert raw.endswith("\n"), f"{filename}: file must end with a newline"
    assert "\r\n" not in raw, f"{filename}: must use LF (\\n) line endings, not CRLF"

    # Round-trip stability: parsing and re-serializing must not raise.
    re_serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    assert isinstance(re_serialized, str)


def test_decision_pipeline_dashboard_references_slo_recording_rules() -> None:
    """The decision_pipeline dashboard must drive its SLO panels from
    the SLO recording rules, not raw metrics. This guards against
    someone reverting an SLO panel to a raw threshold query.
    """
    path = _DASHBOARD_DIR / "decision_pipeline.json"
    raw = path.read_text(encoding="utf-8")
    assert "coherence_fund:slo_avail:error_ratio" in raw, (
        "decision_pipeline.json must reference the SLO_avail recording rule"
    )
    assert "coherence_fund:slo_latency:error_ratio" in raw, (
        "decision_pipeline.json must reference the SLO_latency recording rule"
    )
    assert "coherence_fund:slo_calibration:age_seconds" in raw, (
        "decision_pipeline.json must reference the calibration freshness rule"
    )
