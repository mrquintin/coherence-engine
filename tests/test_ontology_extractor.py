"""Tests for the deterministic ontology extractor."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from coherence_engine.core.types import (
    Entity,
    OntologyEdge,
    OntologyGraph,
    Proposition,
)
from coherence_engine.domain import extract_ontology as domain_extract_ontology
from coherence_engine.domain.ontology import extract_ontology

_FIXTURES = Path(__file__).parent / "fixtures" / "ontology"


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


def _graph_to_jsonable(graph: OntologyGraph) -> dict:
    return {
        "schema_version": graph.schema_version,
        "entities": [dataclasses.asdict(e) for e in graph.entities],
        "edges": [dataclasses.asdict(e) for e in graph.edges],
    }


def test_empty_input_returns_empty_graph_with_schema_version():
    graph = extract_ontology([])
    assert isinstance(graph, OntologyGraph)
    assert graph.entities == ()
    assert graph.edges == ()
    assert graph.schema_version == "ontology-v1"


def test_fixture_is_deterministic_across_runs():
    propositions = _load_propositions("sample_propositions.json")

    first = extract_ontology(propositions)
    second = extract_ontology(propositions)

    dumped_first = json.dumps(_graph_to_jsonable(first), sort_keys=True)
    dumped_second = json.dumps(_graph_to_jsonable(second), sort_keys=True)

    assert dumped_first == dumped_second


def test_fixture_produces_at_least_one_causes_edge():
    propositions = _load_propositions("sample_propositions.json")
    graph = extract_ontology(propositions)

    causes_edges = [e for e in graph.edges if e.relation == "causes"]
    assert causes_edges, "expected at least one 'causes' edge from the fixture"


def test_fixture_exercises_each_entity_type():
    propositions = _load_propositions("sample_propositions.json")
    graph = extract_ontology(propositions)

    types_present = {e.type for e in graph.entities}
    assert {"actor", "metric", "object", "causal_event"}.issubset(types_present)


def test_fixture_exercises_each_relation_type():
    propositions = _load_propositions("sample_propositions.json")
    graph = extract_ontology(propositions)

    relations_present = {e.relation for e in graph.edges}
    expected = {"causes", "depends_on", "part_of", "measures", "competes_with"}
    assert expected.issubset(relations_present), (
        f"missing relations: {expected - relations_present}"
    )


def test_entity_ids_are_stable_sha256_prefix():
    import hashlib

    propositions = [Proposition(id="p", text="Our team drives revenue.")]
    graph = extract_ontology(propositions)

    ids = {e.type: e.id for e in graph.entities}
    expected_actor = hashlib.sha256(b"actor|our team").hexdigest()[:16]
    expected_metric = hashlib.sha256(b"metric|revenue").hexdigest()[:16]
    assert ids["actor"] == expected_actor
    assert ids["metric"] == expected_metric


def test_surface_forms_deduped_case_insensitively():
    propositions = [
        Proposition(id="a", text="We grow revenue."),
        Proposition(id="b", text="WE grow more revenue."),
        Proposition(id="c", text="we grow revenue again."),
    ]
    graph = extract_ontology(propositions)

    actor = next(e for e in graph.entities if e.type == "actor")
    assert {sf.lower() for sf in actor.surface_forms} == {"we"}
    assert actor.mentions == 3


def test_competes_with_prefers_founder_to_named_competitor():
    propositions = [Proposition(id="p", text="Our team faces competitors.")]
    graph = extract_ontology(propositions)

    competes = [e for e in graph.edges if e.relation == "competes_with"]
    assert len(competes) == 1
    types_by_id = {e.id: e.type for e in graph.entities}
    canonicals_by_id = {}
    for e in graph.entities:
        canonicals_by_id[e.id] = e.surface_forms[0].lower()
    assert types_by_id[competes[0].src] == "actor"
    assert canonicals_by_id[competes[0].src] == "our team"
    assert canonicals_by_id[competes[0].dst] == "competitors"


def test_ambiguous_causal_marker_creates_event_entity_without_edge():
    propositions = [Proposition(id="p", text="Therefore growth improves.")]
    graph = extract_ontology(propositions)

    event_entities = [e for e in graph.entities if e.type == "causal_event"]
    assert event_entities, "expected a causal_event entity for a dangling marker"
    assert graph.edges == ()


def test_reexported_from_domain_package():
    assert domain_extract_ontology is extract_ontology


def test_extractor_source_does_not_import_ml_libraries():
    from coherence_engine.domain import ontology as module

    source = Path(module.__file__).read_text()
    banned = ("spacy", "nltk", "transformers", "torch", "sklearn")
    for name in banned:
        assert f"import {name}" not in source
        assert f"from {name}" not in source


def test_edge_evidence_count_aggregates_across_propositions():
    propositions = [
        Proposition(id="a", text="Our team drives revenue."),
        Proposition(id="b", text="Our team drives revenue."),
    ]
    graph = extract_ontology(propositions)

    causes = [e for e in graph.edges if e.relation == "causes"]
    assert len(causes) == 1
    assert causes[0].evidence_count == 2


def test_entity_dataclasses_are_frozen():
    propositions = _load_propositions("sample_propositions.json")
    graph = extract_ontology(propositions)

    assert isinstance(graph, OntologyGraph)
    for e in graph.entities:
        assert isinstance(e, Entity)
    for ed in graph.edges:
        assert isinstance(ed, OntologyEdge)
