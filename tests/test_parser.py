"""Tests for core/parser.py — Argument extraction."""

import pytest
from coherence_engine.core.parser import ArgumentParser
from coherence_engine.core.types import Proposition, ArgumentStructure


@pytest.fixture
def parser():
    return ArgumentParser()


class TestSentenceSplitting:
    def test_basic_splitting(self, parser):
        text = "First sentence. Second sentence. Third sentence."
        structure = parser.parse(text)
        assert structure.n_propositions >= 2

    def test_abbreviations_preserved(self, parser):
        text = "Dr. Smith studies the issue. Mr. Jones disagrees with the findings."
        structure = parser.parse(text)
        assert structure.n_propositions >= 2

    def test_short_fragments_dropped(self, parser):
        text = "OK. This is a much longer real sentence that should be kept."
        structure = parser.parse(text)
        texts = [p.text for p in structure.propositions]
        assert not any(t.strip() == "OK" for t in texts)

    def test_question_mark_splitting(self, parser):
        text = "Is this claim valid? Studies show it may be. Therefore we accept it."
        structure = parser.parse(text)
        assert structure.n_propositions >= 2


class TestClassification:
    def test_claim_detection(self, parser):
        text = "Therefore we conclude that reform is needed. The evidence is clear."
        structure = parser.parse(text)
        claims = structure.claims
        assert len(claims) >= 1

    def test_evidence_detection(self, parser):
        text = "Studies show that exercise improves health. This is a basic fact."
        structure = parser.parse(text)
        evidence = [p for p in structure.propositions if p.prop_type == "evidence"]
        assert len(evidence) >= 1

    def test_qualifier_detection(self, parser):
        text = "The policy is effective. However, there are some limitations to consider."
        structure = parser.parse(text)
        qualifiers = [p for p in structure.propositions if p.prop_type == "qualifier"]
        assert len(qualifiers) >= 1

    def test_default_premise(self, parser):
        text = "Apples are nutritious. Bananas contain potassium."
        structure = parser.parse(text)
        premises = structure.premises
        assert len(premises) >= 1


class TestRelationInference:
    def test_default_supports(self, parser):
        text = "Regular exercise is beneficial. Physical activity reduces stress levels."
        structure = parser.parse(text)
        supports = [r for r in structure.relations if r.relation_type == "supports"]
        assert len(supports) >= 1

    def test_attack_detection(self, parser):
        text = "The policy works well. However, critics point to several flaws."
        structure = parser.parse(text)
        attacks = [r for r in structure.relations if r.relation_type == "attacks"]
        assert len(attacks) >= 1


class TestImportanceScoring:
    def test_claims_highest_importance(self, parser):
        text = "The economy is complex. Therefore we conclude that intervention is needed."
        structure = parser.parse(text)
        claims = structure.claims
        if claims:
            assert claims[0].importance >= 0.7

    def test_importance_range(self, parser):
        text = "Point one is important. Point two follows from it. Thus, we conclude something."
        structure = parser.parse(text)
        for p in structure.propositions:
            assert 0.0 <= p.importance <= 1.0


class TestEdgeCases:
    def test_empty_input(self, parser):
        structure = parser.parse("")
        assert structure.n_propositions == 0

    def test_whitespace_only(self, parser):
        structure = parser.parse("   \n\t  ")
        assert structure.n_propositions == 0

    def test_single_sentence(self, parser):
        structure = parser.parse("This is a single sentence with enough words to pass the filter.")
        assert structure.n_propositions >= 1

    def test_deduplication(self, parser):
        text = "The sky is blue and clear. The sky is blue and clear. Something else entirely."
        structure = parser.parse(text)
        texts = [p.text for p in structure.propositions]
        normalized = [t.lower().strip() for t in texts]
        assert len(normalized) == len(set(normalized))

    def test_max_propositions(self):
        p = ArgumentParser(max_propositions=3)
        text = ". ".join([f"Sentence number {i} with enough words" for i in range(20)])
        structure = p.parse(text)
        assert structure.n_propositions <= 3
