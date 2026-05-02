"""Outcome-labeling service tests (prompt 43).

Covers:

* Schema validation: an outcome row missing ``outcome_provenance`` is rejected
  with :class:`OutcomeSchemaError` and nothing is written to disk.
* Latest-wins selection: when multiple outcomes are attached for the same
  ``pitch_id``, ``export`` keeps the row with the greatest ``outcome_as_of``.
* Audit: a partial outcomes file leaves manifest pitch_ids in
  ``pitches_missing`` (and the report is not ``ok``).
* ``unknown`` exclusion: rows whose latest outcome is unknown are dropped
  from the export by default and kept with ``include_unknown=True``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.server.fund.services.outcome_labeling import (
    AuditReport,
    OutcomeSchemaError,
    SCHEMA_VERSION,
    attach_outcome,
    audit,
    export,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "data" / "historical_corpus" / "manifest.jsonl"


def _load_manifest_pitch_ids() -> list:
    ids = []
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ids.append(row["pitch_id"])
    return ids


def _good_outcome(pitch_id: str, *, as_of: str = "2025-06-01", **overrides) -> dict:
    row = {
        "schema_version": SCHEMA_VERSION,
        "pitch_id": pitch_id,
        "survival_5yr": True,
        "exit_event": "active",
        "last_known_arr_usd": 1_500_000.0,
        "last_known_headcount": 12,
        "outcome_as_of": as_of,
        "outcome_provenance": {
            "source": "crunchbase",
            "url": "https://crunchbase.com/organization/anon",
            "retrieved_at": "2026-04-25T12:00:00+00:00",
            "retrieved_by": "ops@coherence-engine.test",
        },
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# attach_outcome — provenance enforced
# ---------------------------------------------------------------------------


def test_attach_outcome_without_provenance_fails(tmp_path):
    pid = _load_manifest_pitch_ids()[0]
    bad = _good_outcome(pid)
    bad.pop("outcome_provenance")
    out_path = tmp_path / "outcomes.jsonl"

    with pytest.raises(OutcomeSchemaError) as ei:
        attach_outcome(pid, bad, outcomes_path=out_path)

    assert any("outcome_provenance" in e for e in ei.value.errors)
    # Nothing written.
    assert not out_path.exists() or out_path.read_text().strip() == ""


def test_attach_outcome_rejects_unparseable_url(tmp_path):
    pid = _load_manifest_pitch_ids()[0]
    bad = _good_outcome(pid)
    bad["outcome_provenance"]["url"] = "not-a-url"
    out_path = tmp_path / "outcomes.jsonl"

    with pytest.raises(OutcomeSchemaError) as ei:
        attach_outcome(pid, bad, outcomes_path=out_path)

    assert any("url" in e for e in ei.value.errors)


def test_attach_outcome_rejects_pitch_id_mismatch(tmp_path):
    pid = _load_manifest_pitch_ids()[0]
    other = _load_manifest_pitch_ids()[1]
    out_path = tmp_path / "outcomes.jsonl"
    row = _good_outcome(pid)

    with pytest.raises(OutcomeSchemaError):
        attach_outcome(other, row, outcomes_path=out_path)


def test_attach_outcome_writes_valid_row(tmp_path):
    pid = _load_manifest_pitch_ids()[0]
    out_path = tmp_path / "outcomes.jsonl"
    attach_outcome(pid, _good_outcome(pid), outcomes_path=out_path)

    content = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(content) == 1
    parsed = json.loads(content[0])
    assert parsed["pitch_id"] == pid
    assert parsed["outcome_provenance"]["source"] == "crunchbase"


# ---------------------------------------------------------------------------
# export — latest-wins per pitch_id
# ---------------------------------------------------------------------------


def test_export_keeps_latest_by_outcome_as_of(tmp_path):
    pid = _load_manifest_pitch_ids()[0]
    out_path = tmp_path / "outcomes.jsonl"

    earlier = _good_outcome(
        pid, as_of="2024-01-15", exit_event="active", last_known_arr_usd=500_000.0
    )
    later = _good_outcome(
        pid, as_of="2025-08-01", exit_event="acquired", last_known_arr_usd=2_000_000.0
    )
    attach_outcome(pid, earlier, outcomes_path=out_path)
    attach_outcome(pid, later, outcomes_path=out_path)

    frame = export(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    rows = [r for r in frame["rows"] if r["pitch_id"] == pid]
    assert len(rows) == 1
    assert rows[0]["exit_event"] == "acquired"
    assert rows[0]["outcome_as_of"] == "2025-08-01"
    assert rows[0]["last_known_arr_usd"] == 2_000_000.0


def test_export_excludes_unknown_by_default(tmp_path):
    pids = _load_manifest_pitch_ids()[:2]
    out_path = tmp_path / "outcomes.jsonl"

    known = _good_outcome(pids[0], exit_event="active")
    unknown = _good_outcome(pids[1], exit_event="unknown", survival_5yr="unknown")
    attach_outcome(pids[0], known, outcomes_path=out_path)
    attach_outcome(pids[1], unknown, outcomes_path=out_path)

    frame_default = export(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    exported_ids = {r["pitch_id"] for r in frame_default["rows"]}
    assert pids[0] in exported_ids
    assert pids[1] not in exported_ids
    assert frame_default["excluded_unknown"] == 1

    frame_with = export(
        manifest_path=MANIFEST_PATH, outcomes_path=out_path, include_unknown=True
    )
    exported_ids = {r["pitch_id"] for r in frame_with["rows"]}
    assert pids[1] in exported_ids
    assert frame_with["excluded_unknown"] == 0


def test_export_rows_are_pitch_id_sorted(tmp_path):
    pids = _load_manifest_pitch_ids()[:5]
    out_path = tmp_path / "outcomes.jsonl"
    # Attach in reverse order to prove ordering is by pitch_id, not file order.
    for pid in reversed(pids):
        attach_outcome(pid, _good_outcome(pid), outcomes_path=out_path)
    frame = export(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    exported = [r["pitch_id"] for r in frame["rows"]]
    assert exported == sorted(exported)


# ---------------------------------------------------------------------------
# audit — partial coverage surfaces missing pitch_ids
# ---------------------------------------------------------------------------


def test_audit_reports_missing_pitches(tmp_path):
    pids = _load_manifest_pitch_ids()
    out_path = tmp_path / "outcomes.jsonl"
    # Attach outcomes for the first 3 pitches only.
    covered = pids[:3]
    for pid in covered:
        attach_outcome(pid, _good_outcome(pid), outcomes_path=out_path)

    report = audit(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    assert isinstance(report, AuditReport)
    assert report.pitches_total == len(pids)
    assert report.pitches_with_outcome == 3
    assert set(report.pitches_missing) == set(pids) - set(covered)
    assert report.ok is False


def test_audit_passes_when_every_pitch_has_outcome(tmp_path):
    pids = _load_manifest_pitch_ids()
    out_path = tmp_path / "outcomes.jsonl"
    for pid in pids:
        attach_outcome(pid, _good_outcome(pid), outcomes_path=out_path)

    report = audit(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    assert report.pitches_missing == []
    assert report.rows_invalid == []
    assert report.ok is True


def test_audit_flags_invalid_rows(tmp_path):
    pids = _load_manifest_pitch_ids()
    out_path = tmp_path / "outcomes.jsonl"

    # Write a junk row directly so we don't trip attach_outcome's validator.
    out_path.write_text(
        "# header\n"
        + json.dumps({"pitch_id": pids[0], "schema_version": "1"})  # missing fields
        + "\n",
        encoding="utf-8",
    )

    report = audit(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    assert report.rows_invalid
    assert report.ok is False


def test_audit_skips_comment_lines(tmp_path):
    pids = _load_manifest_pitch_ids()
    out_path = tmp_path / "outcomes.jsonl"
    out_path.write_text(
        "# leading comment\n"
        "# another comment\n",
        encoding="utf-8",
    )
    # No data rows: every pitch should be missing, no invalid rows.
    report = audit(manifest_path=MANIFEST_PATH, outcomes_path=out_path)
    assert report.rows_seen == 0
    assert report.rows_invalid == []
    assert len(report.pitches_missing) == len(pids)
