"""Historical-startups validation corpus harness tests (prompt 42).

Covers:

* Schema validation: a tampered seed (extra key, bad UUID, bad URI, bad enum)
  is rejected with structured errors.
* Eligibility computation: pure function maps content → flags as documented.
* Ingestion: synthetic seeds produce the expected accept/reject counts and
  the canonical eligibility flags are written back to the row.
* Consent invariant: non-synthetic rows lacking documented consent are
  rejected; synthetic rows are exempt.
* ``stat`` summary: counts across the seed manifest match a pinned expected
  report.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from coherence_engine.server.fund.services.historical_corpus import (
    DEFAULT_MANIFEST_PATH,
    EligibilityFlags,
    IngestionReport,
    ValidationReport,
    compute_eligibility,
    ingest,
    stat,
    validate,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDS_DIR = REPO_ROOT / "data" / "historical_corpus" / "seeds"


# ---------------------------------------------------------------------------
# Pinned expected stat for the 25 shipped synthetic seeds.
# Must match the deterministic seed generator output. If a seed is
# regenerated, both values change in lockstep.
# ---------------------------------------------------------------------------

EXPECTED_STAT = {
    "total_rows": 25,
    "by_source": {
        "cb_insights": 0,
        "crunchbase": 0,
        "operator_archive": 0,
        "public_filings": 0,
        "synthetic": 25,
    },
    "by_domain": {
        "biotech": 5,
        "climate": 0,
        "consumer": 5,
        "deeptech": 5,
        "edtech": 0,
        "enterprise_saas": 0,
        "fintech": 5,
        "healthtech": 5,
        "marketplace": 0,
        "other": 0,
    },
    "by_year": {
        "2012": 4,
        "2014": 4,
        "2016": 5,
        "2018": 5,
        "2020": 5,
        "2025": 1,
        "2029": 1,
    },
    "eligibility": {
        "all_ok": 11,
        "consent_documented": 25,
        "date_window_ok": 23,
        "evidence_floor_ok": 12,
        "no_training_overlap_ok": 25,
    },
}


def _load_seed(name: str) -> dict:
    with (SEEDS_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _ingest_seeds(target_manifest: Path) -> IngestionReport:
    return ingest(
        SEEDS_DIR,
        source="synthetic",
        dry_run=False,
        manifest_path=target_manifest,
    )


# ---------------------------------------------------------------------------
# compute_eligibility — pure function
# ---------------------------------------------------------------------------


def test_compute_eligibility_returns_flags_dataclass():
    row = _load_seed("seed_03_deeptech.json")
    flags = compute_eligibility(row)
    assert isinstance(flags, EligibilityFlags)
    # seed_03 is in the high-coherence band: should pass every flag.
    assert flags.all_ok is True
    assert flags.to_dict() == {
        "date_window_ok": True,
        "evidence_floor_ok": True,
        "no_training_overlap_ok": True,
        "consent_documented": True,
    }


def test_compute_eligibility_flags_low_evidence_row():
    row = _load_seed("seed_00_fintech.json")
    flags = compute_eligibility(row)
    assert flags.evidence_floor_ok is False
    assert flags.date_window_ok is True


def test_compute_eligibility_flags_out_of_window_year():
    row = _load_seed("seed_05_fintech.json")  # year=2025 — out of window
    assert row["pitch_year"] == 2025
    flags = compute_eligibility(row)
    assert flags.date_window_ok is False


def test_compute_eligibility_real_row_without_consent_fails():
    row = _load_seed("seed_03_deeptech.json")
    row = copy.deepcopy(row)
    row["provenance"]["source"] = "operator_archive"
    row["provenance"]["consent_documented"] = False
    flags = compute_eligibility(row)
    assert flags.consent_documented is False


# ---------------------------------------------------------------------------
# Schema validation — tampered seed must fail
# ---------------------------------------------------------------------------


def test_tampered_seed_fails_schema_validation(tmp_path):
    base = _load_seed("seed_03_deeptech.json")

    # Drop a required key.
    bad = copy.deepcopy(base)
    bad.pop("country")
    p = tmp_path / "bad_missing_key.json"
    p.write_text(json.dumps(bad))

    report = ingest(p, source="synthetic", dry_run=True, manifest_path=tmp_path / "m.jsonl")
    assert report.rows_accepted == 0
    assert report.rows_rejected == 1
    assert "missing required keys" in str(report.rejections[0]["errors"])

    # Bad UUID.
    bad = copy.deepcopy(base)
    bad["pitch_id"] = "not-a-uuid"
    p = tmp_path / "bad_uuid.json"
    p.write_text(json.dumps(bad))
    report = ingest(p, source="synthetic", dry_run=True, manifest_path=tmp_path / "m.jsonl")
    assert report.rows_accepted == 0
    assert any("pitch_id" in e for e in report.rejections[0]["errors"])

    # Bad enum value.
    bad = copy.deepcopy(base)
    bad["domain_primary"] = "crypto-rugpull"
    p = tmp_path / "bad_domain.json"
    p.write_text(json.dumps(bad))
    report = ingest(p, source="synthetic", dry_run=True, manifest_path=tmp_path / "m.jsonl")
    assert report.rows_accepted == 0
    assert any("domain_primary" in e for e in report.rejections[0]["errors"])

    # Extra key (additionalProperties: false).
    bad = copy.deepcopy(base)
    bad["unexpected_extra"] = "boom"
    p = tmp_path / "bad_extra.json"
    p.write_text(json.dumps(bad))
    report = ingest(p, source="synthetic", dry_run=True, manifest_path=tmp_path / "m.jsonl")
    assert report.rows_accepted == 0
    assert any("unexpected" in e for e in report.rejections[0]["errors"])


# ---------------------------------------------------------------------------
# Ingestion — fixtures yield expected eligibility flags
# ---------------------------------------------------------------------------


def test_ingest_seeds_dry_run_does_not_write(tmp_path):
    target = tmp_path / "manifest.jsonl"
    report = ingest(SEEDS_DIR, source="synthetic", dry_run=True, manifest_path=target)
    assert report.candidates_seen == 25
    assert report.rows_accepted == 25
    assert report.rows_rejected == 0
    assert report.rows_written == []
    assert report.dry_run is True
    assert not target.exists()


def test_ingest_seeds_apply_writes_manifest(tmp_path):
    target = tmp_path / "manifest.jsonl"
    report = _ingest_seeds(target)
    assert report.candidates_seen == 25
    assert report.rows_accepted == 25
    assert report.rows_rejected == 0
    assert len(report.rows_written) == 25
    assert target.exists()
    rows = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    assert len(rows) == 25
    # Eligibility on disk equals recomputed eligibility (no drift at ingest).
    for row in rows:
        recomputed = compute_eligibility(row).to_dict()
        assert row["eligibility"] == recomputed
        assert row["provenance"]["source"] == "synthetic"


def test_ingest_is_idempotent_on_duplicate_pitch_ids(tmp_path):
    target = tmp_path / "manifest.jsonl"
    _ingest_seeds(target)
    # Second pass should reject every row as a duplicate.
    second = _ingest_seeds(target)
    assert second.rows_accepted == 0
    assert second.rows_rejected == 25
    assert all(r["reason"].startswith("duplicate") for r in second.rejections)


def test_ingest_rejects_non_synthetic_without_consent(tmp_path):
    base = _load_seed("seed_03_deeptech.json")
    bad = copy.deepcopy(base)
    bad["provenance"]["source"] = "operator_archive"
    bad["provenance"]["consent_documented"] = False
    p = tmp_path / "no_consent.json"
    p.write_text(json.dumps(bad))

    report = ingest(p, source="operator_archive", dry_run=True, manifest_path=tmp_path / "m.jsonl")
    assert report.rows_accepted == 0
    assert report.rows_rejected == 1
    assert "consent_missing" in report.rejections[0]["reason"]


def test_ingest_rejects_unknown_source():
    with pytest.raises(ValueError, match="unknown provenance source"):
        ingest(SEEDS_DIR, source="bogus_source", dry_run=True)


# ---------------------------------------------------------------------------
# stat() — pinned expected report
# ---------------------------------------------------------------------------


def test_stat_matches_pinned_expected_report(tmp_path):
    target = tmp_path / "manifest.jsonl"
    _ingest_seeds(target)
    summary = stat(manifest_path=target)
    # manifest_path varies per run; assert on the rest.
    summary_no_path = {k: v for k, v in summary.items() if k != "manifest_path"}
    assert summary_no_path == EXPECTED_STAT


def test_stat_on_shipped_manifest_matches_pinned():
    """The seed manifest shipped in-tree must keep matching the pinned counts.

    If you regenerate the seeds, regenerate the manifest and update
    ``EXPECTED_STAT`` in the same commit.
    """

    if not DEFAULT_MANIFEST_PATH.exists():
        pytest.skip("shipped manifest not present in this checkout")
    summary = stat()
    summary_no_path = {k: v for k, v in summary.items() if k != "manifest_path"}
    assert summary_no_path == EXPECTED_STAT


# ---------------------------------------------------------------------------
# validate() — re-validates and surfaces eligibility drift
# ---------------------------------------------------------------------------


def test_validate_clean_manifest_returns_no_failures(tmp_path):
    target = tmp_path / "manifest.jsonl"
    _ingest_seeds(target)
    report = validate(manifest_path=target)
    assert isinstance(report, ValidationReport)
    assert report.rows_seen == 25
    assert report.rows_ok == 25
    assert report.rows_failed == 0
    assert report.eligibility_drift == []


def test_validate_detects_eligibility_drift(tmp_path):
    target = tmp_path / "manifest.jsonl"
    _ingest_seeds(target)
    # Tamper: flip a stored flag so it no longer matches recomputed value.
    rows = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    rows[0]["eligibility"]["evidence_floor_ok"] = not rows[0]["eligibility"][
        "evidence_floor_ok"
    ]
    target.write_text("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n")
    report = validate(manifest_path=target)
    assert report.rows_failed == 0  # schema still valid
    assert len(report.eligibility_drift) == 1
    drift = report.eligibility_drift[0]
    assert drift["pitch_id"] == rows[0]["pitch_id"]
    assert drift["stored"] != drift["recomputed"]


def test_validate_flags_corrupted_rows(tmp_path):
    target = tmp_path / "manifest.jsonl"
    target.write_text(
        json.dumps({"schema_version": "1", "pitch_id": "broken"}) + "\n"
        '{"this is": "not closing'
    )
    report = validate(manifest_path=target)
    assert report.rows_seen == 2
    assert report.rows_failed == 2
    assert any("missing required keys" in str(f.get("errors", "")) for f in report.failures)
    assert any("json_decode_error" in f.get("reason", "") for f in report.failures)


def test_validate_missing_manifest_returns_empty_report(tmp_path):
    target = tmp_path / "does_not_exist.jsonl"
    report = validate(manifest_path=target)
    assert report.rows_seen == 0
    assert report.rows_failed == 0
    assert report.failures == []


# ---------------------------------------------------------------------------
# Sanity: shipped seeds are all flagged synthetic and exist.
# ---------------------------------------------------------------------------


def test_shipped_seeds_are_synthetic_only():
    seed_files = sorted(SEEDS_DIR.glob("*.json"))
    assert len(seed_files) == 25
    for sp in seed_files:
        with sp.open("r", encoding="utf-8") as fh:
            row = json.load(fh)
        assert row["provenance"]["source"] == "synthetic", (
            f"seed {sp.name} is not flagged synthetic — real data must never "
            "ship in seeds/"
        )
