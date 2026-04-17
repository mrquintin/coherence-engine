"""Tests for layers/structural.py — Graph-structural analysis."""

import pytest
from coherence_engine.core.types import Proposition, Relation, ArgumentStructure
from coherence_engine.layers.structural import StructuralAnalyzer


@pytest.fixture
def analyzer():
    return StructuralAnalyzer()


def _structure(n_props, relations=None, claim_indices=None):
    """Build a test ArgumentStructure."""
    props = []
    for i in range(n_props):
        ptype = "claim" if (claim_indices and i in claim_indices) else "premise"
        props.append(Proposition(id=f"P{i+1}", text=f"Proposition {i+1}", prop_type=ptype))
    rels = []
    if relations:
        for src, tgt, rtype in relations:
            rels.append(Relation(source_id=f"P{src}", target_id=f"P{tgt}", relation_type=rtype))
    return ArgumentStructure(propositions=props, relations=rels)


class TestConnectivity:
    def test_fully_connected_scores_high(self, analyzer):
        s = _structure(
            4,
            relations=[(1, 2, "supports"), (2, 3, "supports"), (3, 4, "supports")],
            claim_indices=[0],
        )
        result = analyzer.analyze(s)
        assert result.details["connectivity"] >= 0.9

    def test_disconnected_scores_lower(self, analyzer):
        s = _structure(
            4,
            relations=[(1, 2, "supports")],
            claim_indices=[0],
        )
        result = analyzer.analyze(s)
        assert result.details["connectivity"] < 1.0 or result.details["isolation_penalty"] > 0


class TestIsolation:
    def test_no_isolated_nodes(self, analyzer):
        s = _structure(3, relations=[(1, 2, "supports"), (2, 3, "supports")])
        result = analyzer.analyze(s)
        assert result.details["n_isolated"] == 0

    def test_isolated_nodes_penalized(self, analyzer):
        s = _structure(4, relations=[(1, 2, "supports")])
        result = analyzer.analyze(s)
        assert result.details["n_isolated"] >= 1
        assert result.details["isolation_penalty"] > 0


class TestDepth:
    def test_chain_has_depth(self, analyzer):
        s = _structure(
            5,
            relations=[
                (1, 2, "supports"), (2, 3, "supports"),
                (3, 4, "supports"), (4, 5, "supports"),
            ],
        )
        result = analyzer.analyze(s)
        assert result.details["max_depth"] >= 3

    def test_flat_has_low_depth(self, analyzer):
        s = _structure(4, relations=[(1, 2, "supports"), (1, 3, "supports"), (1, 4, "supports")])
        result = analyzer.analyze(s)
        assert result.details["max_depth"] <= 2


class TestCycles:
    def test_no_cycles(self, analyzer):
        s = _structure(3, relations=[(1, 2, "supports"), (2, 3, "supports")])
        result = analyzer.analyze(s)
        assert result.details["n_cycles"] == 0

    def test_cycle_penalized(self, analyzer):
        s = _structure(
            3,
            relations=[
                (1, 2, "supports"), (2, 3, "supports"), (3, 1, "supports"),
            ],
        )
        result = analyzer.analyze(s)
        assert result.details["n_cycles"] >= 1


class TestLayerResult:
    def test_result_format(self, analyzer):
        s = _structure(3, relations=[(1, 2, "supports"), (2, 3, "supports")])
        result = analyzer.analyze(s)
        assert result.name == "structural"
        assert 0.0 <= result.score <= 1.0
        assert result.weight == 0.15

    def test_score_bounded(self, analyzer):
        s = _structure(
            5,
            relations=[
                (1, 2, "supports"), (2, 3, "supports"),
                (3, 4, "supports"), (4, 5, "supports"),
            ],
            claim_indices=[0],
        )
        result = analyzer.analyze(s)
        assert 0.0 <= result.score <= 1.0


class TestEdgeCases:
    def test_single_proposition(self, analyzer):
        s = _structure(1)
        result = analyzer.analyze(s)
        assert result.score == 0.5

    def test_empty_structure(self, analyzer):
        s = ArgumentStructure(propositions=[], relations=[])
        result = analyzer.analyze(s)
        assert result.score == 0.5

    def test_no_claims_uses_first_prop(self, analyzer):
        s = _structure(3, relations=[(1, 2, "supports"), (2, 3, "supports")])
        result = analyzer.analyze(s)
        assert result.score > 0.0
