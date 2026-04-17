"""Reproducible decision_artifact.v1 builder + persistence tests."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from coherence_engine.server.fund import models
from coherence_engine.server.fund.database import Base, SessionLocal, engine
from coherence_engine.server.fund.services.decision_artifact import (
    ARTIFACT_KIND,
    DecisionArtifactValidationError,
    SCHEMA_VERSION,
    build_decision_artifact,
    canonical_artifact_bytes,
    persist_decision_artifact,
    validate_artifact,
)
from coherence_engine.server.fund.services.decision_policy import (
    DECISION_POLICY_VERSION,
)


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "server"
    / "fund"
    / "schemas"
    / "artifacts"
    / "decision_artifact.v1.json"
)


@pytest.fixture(autouse=True)
def _reset_fund_tables():
    os.environ.setdefault("COHERENCE_FUND_AUTH_MODE", "db")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _sample_app_state() -> dict:
    """Deterministic fixture for the artifact builder.

    No wall-clock timestamps: ``occurred_at`` is an authored ISO string.
    """
    return {
        "application_id": "app_artifact_01",
        "session_id": "ivw_artifact_01",
        "occurred_at": "2026-04-17T12:00:00+00:00",
        "inputs": {
            "application": {
                "id": "app_artifact_01",
                "founder_id": "fnd_artifact_01",
                "one_liner": "AI co-pilot for compliance teams.",
                "use_of_funds_summary": "hire two engineers; security review.",
                "requested_check_usd": 250000,
                "domain_primary": "market_economics",
                "compliance_status": "clear",
                "transcript_text": "We help compliance teams ship faster.",
                "transcript_uri": "",
            },
            "session_id": "ivw_artifact_01",
            "scoring": {
                "absolute_coherence": 0.71,
                "baseline_coherence": 0.42,
                "coherence_superiority": 0.29,
                "coherence_superiority_ci95": {"lower": 0.20, "upper": 0.32},
            },
        },
        "scoring": {
            "composite": 0.71,
            "per_layer": {
                "contradiction": 0.80,
                "argumentation": 0.70,
                "embedding": 0.65,
                "compression": 0.60,
                "structural": 0.72,
            },
            "uncertainty": {"lower": 0.20, "upper": 0.32},
            "scoring_version": "scoring-v1.0.0",
        },
        "domain": {
            "weights": [
                {"domain": "market_economics", "weight": 0.7},
                {"domain": "governance", "weight": 0.3},
            ],
            "normative_profile": {
                "rights": 0.10,
                "utilitarian": 0.55,
                "deontic": 0.20,
            },
            "schema_version": "domain-mix-v1",
            "notes": [],
        },
        "ontology_graph_id": "abcdef0123456789",
        "ontology_graph_digest": "0" * 64,
        "decision": {
            "verdict": "pass",
            "cs_superiority": 0.29,
            "cs_required": 0.18,
            "reason_codes": [],
            "decision_policy_version": DECISION_POLICY_VERSION,
        },
    }


def test_schema_file_exists_and_declares_required_keys():
    assert SCHEMA_PATH.exists(), f"missing schema at {SCHEMA_PATH}"
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    schema = json.loads(text)
    required = set(schema.get("required") or [])
    for key in (
        "artifact_id",
        "artifact_kind",
        "schema_version",
        "application_id",
        "session_id",
        "occurred_at",
        "inputs_digest",
        "scoring",
        "domain",
        "ontology_graph_id",
        "ontology_graph_digest",
        "decision",
        "pins",
    ):
        assert key in required, f"schema must require {key}"
    assert schema.get("additionalProperties") is False


def test_build_decision_artifact_is_deterministic_and_byte_identical():
    app_state = _sample_app_state()
    a1 = build_decision_artifact(copy.deepcopy(app_state))
    a2 = build_decision_artifact(copy.deepcopy(app_state))

    assert a1 == a2
    assert a1["inputs_digest"] == a2["inputs_digest"]
    assert a1["artifact_kind"] == ARTIFACT_KIND
    assert a1["schema_version"] == SCHEMA_VERSION

    bytes_a = canonical_artifact_bytes(a1)
    bytes_b = canonical_artifact_bytes(a2)
    assert bytes_a == bytes_b
    assert isinstance(bytes_a, bytes)
    assert len(bytes_a) > 0


def test_inputs_digest_changes_when_inputs_change():
    base = _sample_app_state()
    other = copy.deepcopy(base)
    other["inputs"]["application"]["one_liner"] = "DIFFERENT pitch."
    a_base = build_decision_artifact(base)
    a_other = build_decision_artifact(other)
    assert a_base["inputs_digest"] != a_other["inputs_digest"]
    assert a_base["artifact_id"] != a_other["artifact_id"]


def test_build_decision_artifact_validates_against_schema():
    app_state = _sample_app_state()
    artifact = build_decision_artifact(app_state)
    validate_artifact(artifact)


def test_validate_rejects_artifact_missing_decision_policy_version():
    app_state = _sample_app_state()
    artifact = build_decision_artifact(app_state)
    tampered = copy.deepcopy(artifact)
    del tampered["decision"]["decision_policy_version"]
    with pytest.raises(DecisionArtifactValidationError):
        validate_artifact(tampered)


def test_validate_rejects_unknown_top_level_key():
    artifact = build_decision_artifact(_sample_app_state())
    tampered = copy.deepcopy(artifact)
    tampered["unexpected_top_level"] = "not allowed"
    with pytest.raises(DecisionArtifactValidationError):
        validate_artifact(tampered)


def test_internal_fail_verdict_is_normalized_to_reject():
    app_state = _sample_app_state()
    app_state["decision"]["verdict"] = "fail"
    artifact = build_decision_artifact(app_state)
    assert artifact["decision"]["verdict"] == "reject"


def test_pins_include_scoring_and_decision_policy_versions():
    artifact = build_decision_artifact(_sample_app_state())
    assert artifact["pins"]["scoring_version"] == "scoring-v1.0.0"
    assert artifact["pins"]["decision_policy_version"] == DECISION_POLICY_VERSION
    assert "prompt_registry_digest" not in artifact["pins"]


def test_pins_include_optional_prompt_registry_digest_when_provided():
    app_state = _sample_app_state()
    app_state["prompt_registry_digest"] = "deadbeef" * 8
    artifact = build_decision_artifact(app_state)
    assert artifact["pins"]["prompt_registry_digest"] == "deadbeef" * 8
    validate_artifact(artifact)


def test_persist_decision_artifact_writes_row_with_expected_kind():
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_art1",
            full_name="Artifact Founder",
            email="art@example.com",
            company_name="Artifact Co",
            country="US",
        )
        application = models.Application(
            id="app_artifact_01",
            founder_id="fnd_art1",
            one_liner="AI co-pilot for compliance teams.",
            requested_check_usd=250000,
            use_of_funds_summary="hiring",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
            status="scoring_in_progress",
        )
        scoring_job = models.ScoringJob(
            id="sjob_art1",
            application_id="app_artifact_01",
            mode="full",
            dry_run=False,
            trace_id="trc_art1",
            idempotency_key="idem_art1",
            status="completed",
        )
        db.add_all([founder, application, scoring_job])
        db.commit()

        artifact = build_decision_artifact(_sample_app_state())
        rec = persist_decision_artifact(
            db,
            app_id="app_artifact_01",
            artifact_dict=artifact,
            scoring_job_id="sjob_art1",
        )
        db.commit()

        assert rec.kind == ARTIFACT_KIND
        assert rec.id == artifact["artifact_id"]
        assert rec.payload_json is not None
        reloaded = json.loads(rec.payload_json)
        assert reloaded == artifact

        rows = (
            db.query(models.ArgumentArtifact)
            .filter(models.ArgumentArtifact.application_id == "app_artifact_01")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].kind == ARTIFACT_KIND
    finally:
        db.close()


def test_persist_decision_artifact_validates_before_writing():
    db = SessionLocal()
    try:
        founder = models.Founder(
            id="fnd_art2",
            full_name="Artifact Founder",
            email="art2@example.com",
            company_name="Artifact Co",
            country="US",
        )
        application = models.Application(
            id="app_artifact_02",
            founder_id="fnd_art2",
            one_liner="x",
            requested_check_usd=100000,
            use_of_funds_summary="hiring",
            preferred_channel="web_voice",
            domain_primary="market_economics",
            compliance_status="clear",
        )
        db.add_all([founder, application])
        db.commit()

        bad_state = _sample_app_state()
        bad_state["application_id"] = "app_artifact_02"
        bad_state["session_id"] = "ivw_artifact_02"
        artifact = build_decision_artifact(bad_state)
        del artifact["pins"]

        with pytest.raises(DecisionArtifactValidationError):
            persist_decision_artifact(
                db,
                app_id="app_artifact_02",
                artifact_dict=artifact,
            )

        rows = (
            db.query(models.ArgumentArtifact)
            .filter(models.ArgumentArtifact.application_id == "app_artifact_02")
            .all()
        )
        assert rows == []
    finally:
        db.close()
