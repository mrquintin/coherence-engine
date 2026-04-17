"""Tests for layers/argumentation.py — Dung's framework, grounded extension."""

import pytest
from coherence_engine.core.types import Proposition, Relation, ArgumentStructure, LayerResult
from coherence_engine.layers.argumentation import ArgumentationAnalyzer


@pytest.fixture
def analyzer():
    return ArgumentationAnalyzer()


def _structure(n_props, attack_pairs=None):
    """Build an ArgumentStructure with n propositions and specified attacks."""
    props = [Proposition(id=f"P{i+1}", text=f"Proposition {i+1}") for i in range(n_props)]
    rels = []
    if attack_pairs:
        for src, tgt in attack_pairs:
            rels.append(Relation(source_id=f"P{src}", target_id=f"P{tgt}", relation_type="attacks"))
    return ArgumentStructure(propositions=props, relations=rels)


class TestGroundedExtension:
    def test_no_attacks_all_grounded(self, analyzer):
        """With no attacks, every proposition is in the grounded extension."""
        s = _structure(5)
        result = analyzer.analyze(s)
        assert result.score == 1.0
        assert result.details["grounded_extension_size"] == 5

    def test_simple_attack(self, analyzer):
        """P1 attacks P2: P1 is grounded, P2 is not (unless defended)."""
        s = _structure(2, attack_pairs=[(1, 2)])
        result = analyzer.analyze(s)
        assert "P1" in result.details["grounded_extension"]
        assert "P2" not in result.details["grounded_extension"]

    def test_defense_reinstates(self, analyzer):
        """P1 attacks P2, P3 attacks P1: P2 is reinstated via P3's defense."""
        s = _structure(3, attack_pairs=[(1, 2), (3, 1)])
        result = analyzer.analyze(s)
        grounded = set(result.details["grounded_extension"])
        assert "P3" in grounded
        assert "P2" in grounded
        assert "P1" not in grounded

    def test_mutual_attack_neither_grounded(self, analyzer):
        """P1 ↔ P2: mutual attack, neither is in the grounded extension."""
        s = _structure(2, attack_pairs=[(1, 2), (2, 1)])
        result = analyzer.analyze(s)
        assert result.details["grounded_extension_size"] == 0
        assert result.score == 0.0

    def test_chain_attack(self, analyzer):
        """P1→P2→P3: P1 grounded, P2 out, P3 reinstated."""
        s = _structure(3, attack_pairs=[(1, 2), (2, 3)])
        result = analyzer.analyze(s)
        grounded = set(result.details["grounded_extension"])
        assert "P1" in grounded
        assert "P2" not in grounded
        assert "P3" in grounded


class TestLayerResult:
    def test_result_structure(self, analyzer):
        s = _structure(4, attack_pairs=[(1, 2)])
        result = analyzer.analyze(s)
        assert isinstance(result, LayerResult)
        assert result.name == "argumentation"
        assert 0.0 <= result.score <= 1.0
        assert result.weight == 0.20

    def test_details_present(self, analyzer):
        s = _structure(3, attack_pairs=[(1, 2)])
        result = analyzer.analyze(s)
        assert "n_propositions" in result.details
        assert "n_attack_relations" in result.details
        assert "grounded_extension_size" in result.details
        assert "n_cycles" in result.details

    def test_attack_count(self, analyzer):
        s = _structure(3, attack_pairs=[(1, 2), (2, 3)])
        result = analyzer.analyze(s)
        assert result.details["n_attack_relations"] == 2


class TestCycleDetection:
    def test_no_cycles(self, analyzer):
        s = _structure(3, attack_pairs=[(1, 2)])
        result = analyzer.analyze(s)
        assert result.details["n_cycles"] == 0

    def test_cycle_detected(self, analyzer):
        s = _structure(3, attack_pairs=[(1, 2), (2, 3), (3, 1)])
        result = analyzer.analyze(s)
        assert result.details["n_cycles"] >= 1


class TestEdgeCases:
    def test_single_proposition(self, analyzer):
        s = _structure(1)
        result = analyzer.analyze(s)
        assert result.score == 1.0

    def test_empty_structure(self, analyzer):
        s = ArgumentStructure(propositions=[], relations=[])
        result = analyzer.analyze(s)
        assert result.score == 1.0

    def test_support_relations_ignored_for_attacks(self, analyzer):
        """Support relations should not be treated as attacks."""
        props = [Proposition(id=f"P{i+1}", text=f"Prop {i+1}") for i in range(3)]
        rels = [Relation(source_id="P1", target_id="P2", relation_type="supports")]
        s = ArgumentStructure(propositions=props, relations=rels)
        result = analyzer.analyze(s)
        assert result.score == 1.0
