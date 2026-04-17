"""Deterministic ontology entity + relation extractor.

Stage 4 of the blueprint ("Ideological Ontology and Domain Reconstruction")
consumes an :class:`OntologyGraph` built from compiled propositions. This
module derives that graph using only lexical marker lookups from a bundled
JSON lexicon — no ML, no POS tagging, no external NLP library.

Entities fall into {actor, object, causal_event, metric, other}. Relations
fall into {causes, depends_on, part_of, measures, competes_with}. Given the
same proposition input the output is byte-identical across runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable, Sequence

from coherence_engine.core.types import (
    Entity,
    OntologyEdge,
    OntologyGraph,
    Proposition,
)


_SCHEMA_VERSION = "ontology-v1"


def _lexicon_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "server", "fund", "data", "ontology_lexicon.json")


_DEFAULT_LEXICON: dict = {
    "actor_markers": [],
    "causal_markers": [],
    "metric_markers": [],
    "competition_markers": [],
    "dependency_markers": [],
    "part_markers": [],
    "measurement_markers": [],
    "object_markers": [],
    "founder_forms": [],
}


def _load_lexicon() -> dict:
    try:
        with open(_lexicon_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_LEXICON)
    merged = dict(_DEFAULT_LEXICON)
    merged.update({k: v for k, v in data.items() if k in _DEFAULT_LEXICON})
    return merged


def _entity_id(entity_type: str, canonical: str) -> str:
    key = f"{entity_type}|{canonical}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _find_marker_spans(text_lower: str, markers: Iterable[str]) -> list:
    spans: list = []
    for marker in markers:
        m = marker.lower()
        if not m:
            continue
        idx = 0
        while True:
            pos = text_lower.find(m, idx)
            if pos < 0:
                break
            end = pos + len(m)
            ok_start = pos == 0 or not text_lower[pos - 1].isalnum()
            ok_end = end == len(text_lower) or not text_lower[end].isalnum()
            if ok_start and ok_end:
                spans.append((pos, end, m))
            idx = pos + 1
    return spans


def _longest_non_overlapping(spans: list) -> list:
    ordered = sorted(spans, key=lambda s: (-(s[1] - s[0]), s[0], s[2]))
    chosen: list = []
    for sp in ordered:
        overlap = False
        for c in chosen:
            if not (sp[1] <= c[0] or sp[0] >= c[1]):
                overlap = True
                break
        if not overlap:
            chosen.append(sp)
    chosen.sort(key=lambda s: (s[0], s[1]))
    return chosen


def extract_ontology(propositions: Sequence[Proposition]) -> OntologyGraph:
    """Extract a deterministic :class:`OntologyGraph` from propositions.

    Empty input yields an empty graph with the correct schema_version.
    Relation extraction uses simple left/right splits around marker spans;
    the extractor never resolves direction beyond the lexical order.
    """
    if not propositions:
        return OntologyGraph(entities=(), edges=(), schema_version=_SCHEMA_VERSION)

    lex = _load_lexicon()
    actor_markers = lex["actor_markers"]
    causal_markers = lex["causal_markers"]
    metric_markers = lex["metric_markers"]
    competition_markers = lex["competition_markers"]
    dependency_markers = lex["dependency_markers"]
    part_markers = lex["part_markers"]
    measurement_markers = lex["measurement_markers"]
    object_markers = lex["object_markers"]
    founder_forms = {f.lower() for f in lex["founder_forms"]}

    entity_registry: dict = {}
    edge_registry: dict = {}

    def _register_entity(entity_type: str, canonical: str, surface: str) -> str:
        key = (entity_type, canonical)
        record = entity_registry.get(key)
        if record is None:
            record = {"surface_forms": {}, "mentions": 0}
            entity_registry[key] = record
        record["surface_forms"][surface.lower()] = surface
        record["mentions"] += 1
        return _entity_id(entity_type, canonical)

    def _register_edge(src_id: str, dst_id: str, relation: str) -> None:
        if not src_id or not dst_id or src_id == dst_id:
            return
        key = (src_id, dst_id, relation)
        edge_registry[key] = edge_registry.get(key, 0) + 1

    for prop in propositions:
        text = prop.text or ""
        lower = text.lower()

        raw_entity_spans: list = []
        for entity_type, markers in (
            ("actor", actor_markers),
            ("metric", metric_markers),
            ("object", object_markers),
        ):
            for start, end, marker in _find_marker_spans(lower, markers):
                raw_entity_spans.append((start, end, entity_type, marker))

        # Prefer longer markers; drop overlaps (so "our team" beats "team").
        pseudo = [(s, e, f"{t}|{m}") for (s, e, t, m) in raw_entity_spans]
        kept = _longest_non_overlapping(pseudo)
        kept_keys = {(s, e) for (s, e, _) in kept}

        entities: list = []
        for (start, end, entity_type, marker) in raw_entity_spans:
            if (start, end) not in kept_keys:
                continue
            eid = _register_entity(entity_type, marker, text[start:end])
            entities.append((start, end, entity_type, marker, eid))
        entities.sort(key=lambda e: (e[0], e[1]))

        def _nearest_before(pos: int):
            best = None
            for e in entities:
                if e[1] <= pos:
                    best = e
                else:
                    break
            return best

        def _nearest_after(pos: int):
            for e in entities:
                if e[0] >= pos:
                    return e
            return None

        def _process_relation(markers, relation, entity_type_for_event=None):
            spans = _longest_non_overlapping(_find_marker_spans(lower, markers))
            for start, end, marker in spans:
                if entity_type_for_event:
                    _register_entity(
                        entity_type_for_event, marker, text[start:end]
                    )
                left = _nearest_before(start)
                right = _nearest_after(end)
                if left and right:
                    _register_edge(left[4], right[4], relation)

        _process_relation(
            causal_markers, "causes", entity_type_for_event="causal_event"
        )
        _process_relation(dependency_markers, "depends_on")
        _process_relation(part_markers, "part_of")
        _process_relation(measurement_markers, "measures")

        comp_spans = _longest_non_overlapping(
            _find_marker_spans(lower, competition_markers)
        )
        for start, end, _marker in comp_spans:
            actors_only = [e for e in entities if e[2] == "actor"]
            if len(actors_only) < 2:
                continue
            founders = [e for e in actors_only if e[3] in founder_forms]
            non_founders = [e for e in actors_only if e[3] not in founder_forms]
            if founders and non_founders:
                _register_edge(founders[0][4], non_founders[0][4], "competes_with")
                continue
            ranked = sorted(
                actors_only,
                key=lambda e: (
                    min(abs(e[0] - start), abs(e[1] - end)),
                    e[0],
                ),
            )
            _register_edge(ranked[0][4], ranked[1][4], "competes_with")

    entities_out = []
    for (etype, canonical), record in entity_registry.items():
        eid = _entity_id(etype, canonical)
        surfaces = tuple(
            record["surface_forms"][k]
            for k in sorted(record["surface_forms"].keys())
        )
        entities_out.append(
            Entity(
                id=eid,
                type=etype,
                surface_forms=surfaces,
                mentions=record["mentions"],
            )
        )
    entities_out.sort(key=lambda e: (e.type, e.id))

    edges_out = [
        OntologyEdge(src=src, dst=dst, relation=rel, evidence_count=count)
        for (src, dst, rel), count in edge_registry.items()
    ]
    edges_out.sort(key=lambda e: (e.src, e.dst, e.relation))

    return OntologyGraph(
        entities=tuple(entities_out),
        edges=tuple(edges_out),
        schema_version=_SCHEMA_VERSION,
    )
