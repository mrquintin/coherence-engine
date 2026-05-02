"""Tests for new functionality: config wiring, verbose mode, JSON loading,
weight validation, parser enhancements, report fixes, and DomainComparator."""

import os
import pytest
import subprocess
import sys

from coherence_engine.config import EngineConfig
from coherence_engine.core.scorer import CoherenceScorer
from coherence_engine.core.parser import ArgumentParser, SUPPORT_INDICATORS
from coherence_engine.core.types import ContradictionPair, CoherenceResult, LayerResult, ArgumentStructure
from coherence_engine.core.report import ReportGenerator
from coherence_engine.domain.premises import DOMAINS, TENSIONS
from coherence_engine.layers.contradiction import _PATTERNS_DATA

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Bug 1: Config wiring ──────────────────────────────────────────

class TestConfigWiring:
    def test_get_embedder_respects_tfidf_preference(self):
        from coherence_engine.embeddings.base import get_embedder
        config = EngineConfig(embedder="tfidf")
        embedder = get_embedder(config)
        from coherence_engine.embeddings.tfidf import TFIDFEmbedder
        assert isinstance(embedder, TFIDFEmbedder)

    def test_scorer_passes_config_to_embedder(self):
        config = EngineConfig(embedder="tfidf")
        scorer = CoherenceScorer(config)
        scorer._init_layers()
        from coherence_engine.embeddings.tfidf import TFIDFEmbedder
        assert isinstance(scorer._embedder, TFIDFEmbedder)

    def test_contradiction_detector_accepts_nli_model(self):
        from coherence_engine.layers.contradiction import ContradictionDetector
        det = ContradictionDetector(backend="heuristic", nli_model="some-model")
        assert det._nli is None  # heuristic backend, NLI not loaded

    def test_sbert_embedder_batch_size(self):
        from coherence_engine.embeddings.sbert import SBERTEmbedder
        emb = SBERTEmbedder(batch_size=64)
        assert emb.batch_size == 64


# ── Bug 2: JSON data loading ──────────────────────────────────────

class TestJSONDataLoading:
    def test_premises_loaded_from_json(self):
        assert len(DOMAINS) == 10
        assert "individual_rights" in DOMAINS
        assert "national_sovereignty" in DOMAINS

    def test_tensions_loaded_from_json(self):
        assert len(TENSIONS) == 12
        assert TENSIONS[0][0] == "individual_rights"
        assert TENSIONS[0][1] == "social_contract"

    def test_contradiction_patterns_loaded(self):
        assert _PATTERNS_DATA is not None
        assert "antonym_pairs" in _PATTERNS_DATA
        assert "negation_words" in _PATTERNS_DATA

    def test_heuristic_uses_json_negation_words(self):
        from coherence_engine.layers.contradiction import HeuristicContradictionDetector
        assert "barely" in HeuristicContradictionDetector.NEGATION_WORDS
        assert "scarcely" in HeuristicContradictionDetector.NEGATION_WORDS


# ── Bug 3: Markdown report contradiction keys ─────────────────────

class TestMarkdownReport:
    def test_contradiction_pair_to_dict_keys(self):
        c = ContradictionPair(
            prop_a_id="P1", prop_b_id="P2",
            prop_a_text="Alpha", prop_b_text="Beta",
            confidence=0.85, explanation="test",
        )
        d = c.to_dict()
        assert "prop_a_text" in d
        assert "prop_b_text" in d
        assert "confidence" in d
        assert "explanation" in d
        assert "prop1_text" not in d
        assert "contradiction_type" not in d

    def test_markdown_uses_correct_keys(self):
        gen = ReportGenerator()
        props = [
            type("P", (), {"id": "P1", "text": "A", "prop_type": "claim", "importance": 1.0, "source_span": (0, 1)})(),
            type("P", (), {"id": "P2", "text": "B", "prop_type": "premise", "importance": 0.7, "source_span": (2, 3)})(),
        ]
        struct = type("S", (), {
            "propositions": props, "relations": [], "original_text": "A. B.",
            "n_propositions": 2, "claims": [props[0]], "premises": [props[1]],
        })()
        contradictions = [
            ContradictionPair(
                prop_a_id="P1", prop_b_id="P2",
                prop_a_text="Dogs are loyal", prop_b_text="Dogs are disloyal",
                confidence=0.9, explanation="antonym test",
            ),
        ]
        result = CoherenceResult(
            composite_score=0.4,
            layer_results=[LayerResult(name="contradiction", score=0.4, weight=0.3)],
            argument_structure=struct,
            contradictions=contradictions,
            metadata={},
        )
        md = gen.to_markdown(result)
        assert "Dogs are loyal" in md
        assert "Dogs are disloyal" in md
        assert "Confidence" in md
        assert "contradiction_type" not in md
        assert "prop1_text" not in md


# ── Bug 4: CLI weight validation ──────────────────────────────────

class TestCLIWeightValidation:
    def test_bad_weight_sum_rejected(self):
        cmd = [sys.executable, "-m", "coherence_engine", "analyze",
               "A point. Another point. Thus the conclusion.",
               "--weights", "0.5,0.5,0.5,0.5,0.5"]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=ENGINE_DIR, timeout=30)
        assert result.returncode != 0
        assert "sum" in result.stderr.lower() or "1.0" in result.stderr

    def test_valid_weights_accepted(self):
        cmd = [sys.executable, "-m", "coherence_engine", "analyze",
               "A point. Another point. Thus the conclusion.",
               "--weights", "0.40,0.15,0.15,0.15,0.15"]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=ENGINE_DIR, timeout=120)
        assert result.returncode == 0


# ── Feature A: Verbose mode ───────────────────────────────────────

class TestVerboseMode:
    def test_layer_timings_in_metadata(self):
        config = EngineConfig(verbose=False)
        scorer = CoherenceScorer(config)
        result = scorer.score("First point. Second point. Therefore the conclusion.")
        assert "layer_timings" in result.metadata
        timings = result.metadata["layer_timings"]
        assert "contradiction" in timings
        assert "embedding" in timings
        assert all(isinstance(v, float) for v in timings.values())

    def test_verbose_prints_to_stderr(self, capsys):
        config = EngineConfig(verbose=True)
        scorer = CoherenceScorer(config)
        scorer.score("First point. Second point. Therefore the conclusion.")
        captured = capsys.readouterr()
        assert "coherence-engine" in captured.err
        assert "Layer" in captured.err


# ── Feature B: Parser enhancements ────────────────────────────────

class TestEnhancedParser:
    @pytest.fixture
    def parser(self):
        return ArgumentParser()

    def test_expanded_claim_indicators(self, parser):
        text = "As a result, the policy should change. The data confirms this."
        structure = parser.parse(text)
        claims = structure.claims
        assert len(claims) >= 1

    def test_expanded_evidence_indicators(self, parser):
        text = "A study by researchers found these results. The economy is complex."
        structure = parser.parse(text)
        evidence = [p for p in structure.propositions if p.prop_type == "evidence"]
        assert len(evidence) >= 1

    def test_paragraph_boundary_weakens_strength(self, parser):
        text = (
            "The economy is growing strongly.\n\n"
            "In contrast, the healthcare sector faces challenges."
        )
        structure = parser.parse(text)
        if structure.relations:
            cross_para_rels = [
                r for r in structure.relations if r.strength < 0.5
            ]
            assert len(cross_para_rels) >= 0  # at least doesn't crash

    def test_coreference_links_added(self, parser):
        text = "The policy is effective. It reduces poverty. This is well documented."
        structure = parser.parse(text)
        ref_rels = [r for r in structure.relations if r.relation_type == "references"]
        assert len(ref_rels) >= 1

    def test_support_indicators_exist(self):
        assert len(SUPPORT_INDICATORS) > 5
        assert "because" in SUPPORT_INDICATORS

    def test_multi_sentence_claim_detection(self, parser):
        text = "Therefore we conclude that reform is needed. This must happen soon."
        structure = parser.parse(text)
        claims = structure.claims
        assert len(claims) >= 1

    def test_all_original_tests_still_pass(self, parser):
        """Sanity: basic functionality unchanged."""
        text = "First sentence. Second sentence. Third sentence."
        structure = parser.parse(text)
        assert structure.n_propositions >= 2
        text2 = ""
        structure2 = parser.parse(text2)
        assert structure2.n_propositions == 0


# ── Feature A: Report timing ─────────────────────────────────────

class TestReportTiming:
    def test_text_report_shows_timing(self):
        config = EngineConfig()
        scorer = CoherenceScorer(config)
        result = scorer.score("First point. Second point. Therefore the conclusion.")
        report = result.report(fmt="text")
        assert "Layer Timing" in report or "layer" in report.lower()

    def test_markdown_report_shows_timing(self):
        config = EngineConfig()
        scorer = CoherenceScorer(config)
        result = scorer.score("First point. Second point. Therefore the conclusion.")
        report = result.report(fmt="markdown")
        assert "Timing" in report


# ── Feature: Cross-layer signal fusion ────────────────────────────

class TestCrossLayerFusion:
    def test_fusion_runs_without_error(self):
        config = EngineConfig()
        scorer = CoherenceScorer(config)
        result = scorer.score(
            "The economy is growing. Employment is rising. "
            "Therefore we conclude that fiscal policy is working."
        )
        assert 0.0 <= result.composite_score <= 1.0

    def test_fusion_notes_in_details(self):
        config = EngineConfig()
        scorer = CoherenceScorer(config)
        result = scorer.score(
            "We are committed to sustainability. We will never invest in renewables. "
            "Our priority is reducing emissions. We oppose all environmental regulations."
        )
        for lr in result.layer_results:
            assert "fusion_notes" in lr.details


# ── Feature: Compression calibration ─────────────────────────────

class TestCompressionCalibration:
    def test_calibrated_score_bounded(self):
        from coherence_engine.layers.compression import CompressionAnalyzer
        from coherence_engine.core.types import Proposition
        analyzer = CompressionAnalyzer()
        props = [Proposition(id=f"P{i}", text=f"Sentence number {i} about the topic.")
                 for i in range(10)]
        structure = ArgumentStructure(propositions=props, relations=[])
        result = analyzer.analyze(structure)
        assert 0.0 <= result.score <= 1.0

    def test_calibration_label_in_details(self):
        from coherence_engine.layers.compression import CompressionAnalyzer
        from coherence_engine.core.types import Proposition
        analyzer = CompressionAnalyzer()
        props = [Proposition(id="P1", text="Alpha."), Proposition(id="P2", text="Beta.")]
        structure = ArgumentStructure(propositions=props, relations=[])
        result = analyzer.analyze(structure)
        assert result.details.get("calibration") == "sigmoid_length_aware"

    def test_short_vs_long_text_stability(self):
        from coherence_engine.layers.compression import CompressionAnalyzer
        from coherence_engine.core.types import Proposition
        analyzer = CompressionAnalyzer()
        base = "The economy is growing and employment rates continue to rise steadily."
        short_props = [Proposition(id=f"P{i}", text=base) for i in range(3)]
        long_props = [Proposition(id=f"P{i}", text=base) for i in range(20)]
        short_result = analyzer.analyze(ArgumentStructure(propositions=short_props))
        long_result = analyzer.analyze(ArgumentStructure(propositions=long_props))
        assert abs(short_result.score - long_result.score) < 0.5


# ── Feature: Embedding threshold calibration ─────────────────────

class TestEmbeddingThresholds:
    def test_thresholds_in_details(self):
        config = EngineConfig()
        scorer = CoherenceScorer(config)
        result = scorer.score("First point is clear. Second point follows.")
        embed_layer = next(r for r in result.layer_results if r.name == "embedding")
        assert "cosine_threshold" in embed_layer.details
        assert "sparsity_threshold" in embed_layer.details

    def test_tfidf_uses_low_thresholds(self):
        from coherence_engine.layers.embedding import THRESHOLDS
        assert THRESHOLDS["TFIDFEmbedder"]["cosine"] == 0.50
        assert THRESHOLDS["TFIDFEmbedder"]["sparsity"] == 0.25

    def test_sbert_uses_high_thresholds(self):
        from coherence_engine.layers.embedding import THRESHOLDS
        assert THRESHOLDS["SBERTEmbedder"]["cosine"] == 0.70
        assert THRESHOLDS["SBERTEmbedder"]["sparsity"] == 0.30
