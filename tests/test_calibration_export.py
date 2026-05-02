"""Tests for calibration_export: production scoring → governed export rows."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


from coherence_engine.server.fund.services.calibration_export import (
    _derive_n_contradictions,
    build_export_rows,
    export_rows_to_json,
    export_rows_to_jsonl,
    extract_scored_events_from_outbox_rows,
    load_outcomes_annotations,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_EVENT = {
    "application_id": "app_abc123",
    "coherence_result_id": "coh_001",
    "absolute_coherence": 0.72,
    "baseline_coherence": 0.50,
    "coherence_superiority": 0.22,
    "coherence_superiority_ci95": {"lower": 0.10, "upper": 0.34},
    "layer_scores": {
        "contradiction": 0.55,
        "argumentation": 0.60,
        "embedding": 0.50,
        "compression": 0.48,
        "structural": 0.52,
    },
    "anti_gaming_score": 0.15,
    "transcript_quality_score": 0.92,
    "n_propositions": 12,
    "n_contradictions": 1,
    "model_versions": {"embedder": "tfidf", "contradiction_backend": "heuristic"},
}

_SAMPLE_OUTCOMES = {"app_abc123": 0.25}


def _make_event(**overrides):
    e = dict(_SAMPLE_EVENT)
    e.update(overrides)
    return e


# ---------------------------------------------------------------------------
# _derive_n_contradictions
# ---------------------------------------------------------------------------

class TestDeriveNContradictions:
    def test_zero_anti_gaming(self):
        assert _derive_n_contradictions(0.0, 12) == 0

    def test_known_inverse(self):
        n_props = 12
        nc = 2
        denom = max(1.0, n_props / 3.0)
        ag = min(1.0, (nc / denom) * 0.5)
        assert _derive_n_contradictions(ag, n_props) == nc

    def test_capped_at_one(self):
        nc = _derive_n_contradictions(1.0, 6)
        assert nc >= 0


# ---------------------------------------------------------------------------
# load_outcomes_annotations
# ---------------------------------------------------------------------------

class TestLoadOutcomesAnnotations:
    def test_flat_object(self, tmp_path):
        p = tmp_path / "outcomes.json"
        p.write_text(json.dumps({"app_1": 0.1, "app_2": 0.2}))
        out = load_outcomes_annotations(p)
        assert out == {"app_1": 0.1, "app_2": 0.2}

    def test_array(self, tmp_path):
        p = tmp_path / "outcomes.json"
        p.write_text(json.dumps([
            {"application_id": "app_1", "outcome_superiority": 0.1},
            {"application_id": "app_2", "outcome_superiority": 0.2},
        ]))
        out = load_outcomes_annotations(p)
        assert out == {"app_1": 0.1, "app_2": 0.2}

    def test_jsonl(self, tmp_path):
        p = tmp_path / "outcomes.jsonl"
        lines = [
            json.dumps({"application_id": "app_1", "outcome_superiority": 0.1}),
            json.dumps({"application_id": "app_2", "outcome_superiority": 0.2}),
        ]
        p.write_text("\n".join(lines) + "\n")
        out = load_outcomes_annotations(p)
        assert out == {"app_1": 0.1, "app_2": 0.2}

    def test_empty(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("")
        assert load_outcomes_annotations(p) == {}

    def test_single_record_object(self, tmp_path):
        p = tmp_path / "single.json"
        p.write_text(json.dumps({"application_id": "app_x", "outcome_superiority": 0.5}))
        out = load_outcomes_annotations(p)
        assert out == {"app_x": 0.5}


# ---------------------------------------------------------------------------
# build_export_rows
# ---------------------------------------------------------------------------

class TestBuildExportRows:
    def test_basic_join(self):
        result = build_export_rows([_SAMPLE_EVENT], _SAMPLE_OUTCOMES)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row["coherence_superiority"] == 0.22
        assert row["outcome_superiority"] == 0.25
        assert row["n_propositions"] == 12
        assert row["transcript_quality"] == 0.92
        assert row["n_contradictions"] == 1
        assert "contradiction" in row["layer_scores"]

    def test_skipped_no_outcome(self):
        result = build_export_rows([_SAMPLE_EVENT], {})
        assert len(result.rows) == 0
        assert result.skipped_no_outcome == 1
        assert result.skipped_invalid == 0

    def test_skipped_invalid_event(self):
        bad = {"application_id": "app_bad"}
        result = build_export_rows([bad], {"app_bad": 0.1})
        assert len(result.rows) == 0
        assert result.skipped_invalid == 1

    def test_missing_application_id(self):
        bad = {"coherence_superiority": 0.1}
        result = build_export_rows([bad], {"": 0.1})
        assert result.skipped_invalid == 1

    def test_derive_n_contradictions_from_anti_gaming(self):
        evt = _make_event()
        del evt["n_contradictions"]
        result = build_export_rows([evt], _SAMPLE_OUTCOMES)
        assert len(result.rows) == 1
        assert result.rows[0]["n_contradictions"] >= 0

    def test_require_standard_layer_keys_pass(self):
        result = build_export_rows(
            [_SAMPLE_EVENT], _SAMPLE_OUTCOMES, require_all_layer_keys=True
        )
        assert len(result.rows) == 1

    def test_require_standard_layer_keys_fail(self):
        evt = _make_event(layer_scores={"contradiction": 0.5})
        result = build_export_rows(
            [evt], _SAMPLE_OUTCOMES, require_all_layer_keys=True
        )
        assert len(result.rows) == 0
        assert result.skipped_invalid == 1

    def test_multiple_events_partial_outcomes(self):
        e1 = _make_event(application_id="app_1")
        e2 = _make_event(application_id="app_2")
        e3 = _make_event(application_id="app_3")
        outcomes = {"app_1": 0.1, "app_3": 0.3}
        result = build_export_rows([e1, e2, e3], outcomes)
        assert len(result.rows) == 2
        assert result.skipped_no_outcome == 1


# ---------------------------------------------------------------------------
# export_rows_to_json / jsonl
# ---------------------------------------------------------------------------

class TestExportSerialization:
    def test_json_roundtrip(self):
        result = build_export_rows([_SAMPLE_EVENT], _SAMPLE_OUTCOMES)
        text = export_rows_to_json(result.rows)
        parsed = json.loads(text)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["coherence_superiority"] == 0.22

    def test_jsonl_roundtrip(self):
        result = build_export_rows([_SAMPLE_EVENT], _SAMPLE_OUTCOMES)
        text = export_rows_to_jsonl(result.rows)
        lines = [l for l in text.strip().split("\n") if l.strip()]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["outcome_superiority"] == 0.25


# ---------------------------------------------------------------------------
# extract_scored_events_from_outbox_rows
# ---------------------------------------------------------------------------

class TestExtractFromOutbox:
    def test_filters_coherence_scored(self):
        rows = [
            {"event_type": "CoherenceScored", "payload_json": json.dumps(_SAMPLE_EVENT)},
            {"event_type": "DecisionIssued", "payload_json": "{}"},
        ]
        out = extract_scored_events_from_outbox_rows(rows)
        assert len(out) == 1
        assert out[0]["coherence_superiority"] == 0.22

    def test_handles_parsed_payload(self):
        rows = [{"event_type": "CoherenceScored", "payload": dict(_SAMPLE_EVENT)}]
        out = extract_scored_events_from_outbox_rows(rows)
        assert len(out) == 1


# ---------------------------------------------------------------------------
# CLI smoke test (subprocess, no DB)
# ---------------------------------------------------------------------------

class TestCLIExportHistoricalOutcomes:
    def test_cli_export_json(self, tmp_path):
        events = tmp_path / "events.json"
        events.write_text(json.dumps([_SAMPLE_EVENT]))
        outcomes = tmp_path / "outcomes.json"
        outcomes.write_text(json.dumps(_SAMPLE_OUTCOMES))
        output = tmp_path / "export.json"

        proc = subprocess.run(
            [
                sys.executable, "-m", "coherence_engine",
                "uncertainty-profile", "export-historical-outcomes",
                "--scored-events", str(events),
                "--outcomes", str(outcomes),
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1].parent)},
        )
        assert proc.returncode == 0, proc.stderr
        summary = json.loads(proc.stdout)
        assert summary["rows_exported"] == 1

        exported = json.loads(output.read_text())
        assert len(exported) == 1
        assert exported[0]["coherence_superiority"] == 0.22
        assert exported[0]["outcome_superiority"] == 0.25

    def test_cli_export_jsonl(self, tmp_path):
        events = tmp_path / "events.json"
        events.write_text(json.dumps([_SAMPLE_EVENT]))
        outcomes = tmp_path / "outcomes.json"
        outcomes.write_text(json.dumps(_SAMPLE_OUTCOMES))
        output = tmp_path / "export.jsonl"

        proc = subprocess.run(
            [
                sys.executable, "-m", "coherence_engine",
                "uncertainty-profile", "export-historical-outcomes",
                "--scored-events", str(events),
                "--outcomes", str(outcomes),
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1].parent)},
        )
        assert proc.returncode == 0, proc.stderr
        lines = [l for l in output.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 1

    def test_cli_export_with_require_layer_keys(self, tmp_path):
        events = tmp_path / "events.json"
        events.write_text(json.dumps([_SAMPLE_EVENT]))
        outcomes = tmp_path / "outcomes.json"
        outcomes.write_text(json.dumps(_SAMPLE_OUTCOMES))
        output = tmp_path / "export.json"

        proc = subprocess.run(
            [
                sys.executable, "-m", "coherence_engine",
                "uncertainty-profile", "export-historical-outcomes",
                "--scored-events", str(events),
                "--outcomes", str(outcomes),
                "--output", str(output),
                "--require-standard-layer-keys",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1].parent)},
        )
        assert proc.returncode == 0, proc.stderr
        summary = json.loads(proc.stdout)
        assert summary["rows_exported"] == 1


# ---------------------------------------------------------------------------
# Deploy script smoke test
# ---------------------------------------------------------------------------

class TestDeployScriptExport:
    def test_deploy_script(self, tmp_path):
        events = tmp_path / "events.json"
        events.write_text(json.dumps([_SAMPLE_EVENT]))
        outcomes = tmp_path / "outcomes.json"
        outcomes.write_text(json.dumps(_SAMPLE_OUTCOMES))
        output = tmp_path / "export.json"

        script = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "export_historical_outcomes.py"
        proc = subprocess.run(
            [
                sys.executable, str(script),
                "--scored-events", str(events),
                "--outcomes", str(outcomes),
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        summary = json.loads(proc.stdout)
        assert summary["rows_exported"] == 1


# ---------------------------------------------------------------------------
# Exported rows pass validate-historical-export round-trip
# ---------------------------------------------------------------------------

class TestExportValidationRoundTrip:
    def test_exported_rows_validate(self):
        from coherence_engine.server.fund.services.governed_historical_dataset import (
            validate_historical_outcomes_export,
        )

        result = build_export_rows([_SAMPLE_EVENT], _SAMPLE_OUTCOMES)
        assert len(result.rows) == 1

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(result.rows, f)
            f.flush()
            p = Path(f.name)

        try:
            val = validate_historical_outcomes_export(p, require_standard_layer_keys=True)
            assert val.ok, f"Validation failed: {val.errors}"
            assert val.valid_rows == 1
        finally:
            p.unlink(missing_ok=True)
