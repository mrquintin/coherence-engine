"""Tests for the hybrid domain-mix detector (detect_domain_mix)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.core.types import (
    DomainMix,
    NormativeProfile,
    OntologyGraph,
    Proposition,
)
from coherence_engine.domain.detector import (
    DomainDetector,
    detect_domain_mix,
)
from coherence_engine.domain.normative import compute_normative_profile
from coherence_engine.domain.ontology import extract_ontology
from coherence_engine.embeddings.tfidf import TFIDFEmbedder

_FIXTURES = Path(__file__).parent / "fixtures" / "hybrid_domain"


def _load_propositions(name: str):
    payload = json.loads((_FIXTURES / name).read_text())
    return [
        Proposition(
            id=p["id"],
            text=p["text"],
            prop_type=p.get("prop_type", "premise"),
        )
        for p in payload["propositions"]
    ]


def _weights_sum(mix: DomainMix) -> float:
    return sum(w for _, w in mix.weights)


def test_single_domain_api_preserved():
    detector = DomainDetector(embedder=TFIDFEmbedder(max_features=200))
    propositions = _load_propositions("rights_argument.json")
    texts = [p.text for p in propositions]
    result = detector.detect(texts, top_k=3)
    assert isinstance(result, list)
    assert len(result) <= 3
    for key, score in result:
        assert isinstance(key, str)
        assert isinstance(score, float)


def test_rights_fixture_returns_valid_domain_mix():
    propositions = _load_propositions("rights_argument.json")
    ontology = extract_ontology(propositions)
    mix = detect_domain_mix(
        propositions,
        ontology,
        embedder=TFIDFEmbedder(max_features=200),
        top_k=3,
    )
    assert isinstance(mix, DomainMix)
    assert mix.schema_version == "domain-mix-v1"
    assert isinstance(mix.normative, NormativeProfile)
    assert 0 < len(mix.weights) <= 3
    assert _weights_sum(mix) == pytest.approx(1.0, abs=1e-6)
    # All weights must be non-negative and ordered descending.
    prev = float("inf")
    for _, w in mix.weights:
        assert w >= 0.0
        assert w <= prev + 1e-9
        prev = w


def test_deterministic_mix_for_same_input():
    propositions = _load_propositions("rights_argument.json")
    ontology = extract_ontology(propositions)

    embedder_a = TFIDFEmbedder(max_features=200)
    embedder_b = TFIDFEmbedder(max_features=200)

    mix_a = detect_domain_mix(propositions, ontology, embedder=embedder_a, top_k=3)
    mix_b = detect_domain_mix(propositions, ontology, embedder=embedder_b, top_k=3)

    assert mix_a.weights == mix_b.weights
    assert mix_a.normative == mix_b.normative
    assert mix_a.ontology_graph_id == mix_b.ontology_graph_id
    assert mix_a.notes == mix_b.notes


def test_tfidf_fallback_path_yields_valid_mix():
    propositions = _load_propositions("market_argument.json")
    ontology = extract_ontology(propositions)

    # Force the TF-IDF fallback explicitly.
    mix = detect_domain_mix(
        propositions,
        ontology,
        embedder=TFIDFEmbedder(max_features=150),
        top_k=3,
    )
    assert isinstance(mix, DomainMix)
    assert 0 < len(mix.weights) <= 3
    assert _weights_sum(mix) == pytest.approx(1.0, abs=1e-6)
    # Market fixture should place market_economics in top-k.
    domain_ids = [d for d, _ in mix.weights]
    assert "market_economics" in domain_ids


def test_missing_ontology_adds_signal_skipped_note():
    propositions = _load_propositions("rights_argument.json")
    empty_ontology = OntologyGraph(entities=(), edges=(), schema_version="ontology-v1")

    mix = detect_domain_mix(
        propositions,
        empty_ontology,
        embedder=TFIDFEmbedder(max_features=200),
        top_k=3,
    )
    assert mix.notes is not None
    assert "signal_skipped: ontology" in mix.notes
    assert _weights_sum(mix) == pytest.approx(1.0, abs=1e-6)


def test_none_ontology_also_skips_ontology_signal():
    propositions = _load_propositions("rights_argument.json")
    mix = detect_domain_mix(
        propositions,
        None,
        embedder=TFIDFEmbedder(max_features=200),
        top_k=3,
    )
    assert mix.notes is not None
    assert "signal_skipped: ontology" in mix.notes


def test_weights_sum_equals_one_with_variable_top_k():
    propositions = _load_propositions("market_argument.json")
    ontology = extract_ontology(propositions)
    for k in (1, 2, 3, 5):
        mix = detect_domain_mix(
            propositions,
            ontology,
            embedder=TFIDFEmbedder(max_features=200),
            top_k=k,
        )
        assert _weights_sum(mix) == pytest.approx(1.0, abs=1e-6)
        assert len(mix.weights) <= max(1, k)


def test_empty_propositions_returns_domain_mix_with_notes():
    mix = detect_domain_mix([], None, embedder=TFIDFEmbedder(max_features=50), top_k=3)
    assert isinstance(mix, DomainMix)
    # All signals must be skipped → weights empty, notes populated.
    assert mix.weights == ()
    assert mix.notes is not None
    assert "signal_skipped: topic" in mix.notes
    assert "signal_skipped: premise" in mix.notes
    assert "signal_skipped: ontology" in mix.notes
    assert "signal_skipped: normative" in mix.notes


def test_domain_mix_top_accessor_returns_highest_weight_entry():
    propositions = _load_propositions("market_argument.json")
    ontology = extract_ontology(propositions)
    mix = detect_domain_mix(
        propositions,
        ontology,
        embedder=TFIDFEmbedder(max_features=200),
        top_k=3,
    )
    top = mix.top()
    assert top is not None
    assert top == mix.weights[0]


def test_ontology_graph_id_present_when_ontology_has_entities():
    propositions = _load_propositions("market_argument.json")
    ontology = extract_ontology(propositions)
    mix = detect_domain_mix(
        propositions,
        ontology,
        embedder=TFIDFEmbedder(max_features=200),
        top_k=3,
    )
    if ontology.entities:
        assert isinstance(mix.ontology_graph_id, str)
        assert len(mix.ontology_graph_id) == 16


def test_compute_normative_profile_from_rights_text():
    propositions = _load_propositions("rights_argument.json")
    profile = compute_normative_profile(propositions)
    assert isinstance(profile, NormativeProfile)
    assert 0.0 <= profile.rights <= 1.0
    assert 0.0 <= profile.utilitarian <= 1.0
    assert 0.0 <= profile.deontic <= 1.0
    # Rights-heavy fixture should score higher on rights axis.
    assert profile.rights > 0.0
    assert profile.rights >= profile.utilitarian


def test_normative_profile_empty_input():
    profile = compute_normative_profile([])
    assert profile == NormativeProfile(rights=0.0, utilitarian=0.0, deontic=0.0)
