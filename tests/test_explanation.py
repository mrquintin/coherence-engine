"""Tests for the Explanation Engine (core/explanation.py)."""

import pytest
from coherence_engine.core.explanation import ExplanationGenerator
from coherence_engine.core.types import (
    CoherenceResult, LayerResult, ArgumentStructure,
    Proposition, ContradictionPair,
)


@pytest.fixture
def explainer():
    return ExplanationGenerator()


def _make_result(
    composite=0.6,
    layer_scores=None,
    contradictions=None,
    n_propositions=5,
):
    """Build a minimal CoherenceResult for testing."""
    defaults = {
        "contradiction": (0.8, 0.30),
        "argumentation": (0.7, 0.20),
        "embedding": (0.6, 0.20),
        "compression": (0.5, 0.15),
        "structural": (0.5, 0.15),
    }
    if layer_scores:
        for k, v in layer_scores.items():
            defaults[k] = (v, defaults[k][1])

    layers = []
    for name, (score, weight) in defaults.items():
        details = {}
        if name == "argumentation":
            details = {
                "n_propositions": n_propositions,
                "grounded_extension_size": int(score * n_propositions),
                "grounded_extension": [f"P{i+1}" for i in range(int(score * n_propositions))],
                "n_attack_relations": 2,
                "n_cycles": 0,
            }
        elif name == "embedding":
            details = {
                "avg_cosine_similarity": score,
                "suspicious_pairs": 0 if score > 0.5 else 3,
                "total_pairs": 10,
            }
        elif name == "compression":
            details = {
                "compression_ratio": 1.0 - score * 0.1,
                "redundancy": 0.1,
            }
        elif name == "structural":
            details = {
                "n_isolated": 0 if score > 0.5 else 3,
                "connectivity": score,
                "max_depth": 3 if score > 0.5 else 1,
                "n_cycles": 0,
            }
        elif name == "contradiction":
            details = {
                "n_contradictions": len(contradictions) if contradictions else 0,
            }
        layers.append(LayerResult(name=name, score=score, weight=weight, details=details))

    props = [Proposition(id=f"P{i+1}", text=f"Prop {i+1}") for i in range(n_propositions)]
    structure = ArgumentStructure(propositions=props, relations=[])
    return CoherenceResult(
        composite_score=composite,
        layer_results=layers,
        argument_structure=structure,
        contradictions=contradictions or [],
    )


class TestExplanationBasics:
    def test_returns_list(self, explainer):
        result = _make_result()
        out = explainer.explain(result)
        assert isinstance(out, list)

    def test_high_score_no_issues(self, explainer):
        result = _make_result(
            composite=0.85,
            layer_scores={"contradiction": 0.9, "argumentation": 0.9,
                          "embedding": 0.8, "compression": 0.7,
                          "structural": 0.8},
        )
        out = explainer.explain(result)
        assert any("highly coherent" in x.lower() for x in out)

    def test_explain_text_format(self, explainer):
        result = _make_result(
            composite=0.3,
            layer_scores={"structural": 0.2},
        )
        text = explainer.explain_text(result)
        assert isinstance(text, str)
        assert "1." in text


class TestContradictionExplanations:
    def test_contradiction_listed(self, explainer):
        contradictions = [
            ContradictionPair(
                prop_a_id="P1", prop_b_id="P2",
                prop_a_text="Cats are friendly.",
                prop_b_text="Cats are hostile.",
                confidence=0.9,
                explanation="Antonym: friendly vs hostile",
            ),
        ]
        result = _make_result(
            composite=0.4,
            layer_scores={"contradiction": 0.3},
            contradictions=contradictions,
        )
        out = explainer.explain(result)
        assert any("Cats are friendly" in x for x in out)
        assert any("Cats are hostile" in x for x in out)


class TestStructuralExplanations:
    def test_isolated_nodes_explained(self, explainer):
        result = _make_result(
            composite=0.3,
            layer_scores={"structural": 0.2},
        )
        out = explainer.explain(result)
        assert any("no supporting evidence" in x.lower() or "connections" in x.lower() for x in out)

    def test_low_connectivity_explained(self, explainer):
        result = _make_result(
            composite=0.3,
            layer_scores={"structural": 0.3},
        )
        out = explainer.explain(result)
        assert any("reachable" in x.lower() or "fragmented" in x.lower() for x in out)


class TestArgumentationExplanations:
    def test_undefended_propositions(self, explainer):
        result = _make_result(
            composite=0.3,
            layer_scores={"argumentation": 0.2},
        )
        out = explainer.explain(result)
        assert any("grounded extension" in x.lower() for x in out)


class TestEmbeddingExplanations:
    def test_low_similarity_explained(self, explainer):
        result = _make_result(
            composite=0.3,
            layer_scores={"embedding": 0.2},
        )
        result.layer_results[2].details["avg_cosine_similarity"] = 0.15
        out = explainer.explain(result)
        assert any("similarity" in x.lower() for x in out)
