"""Tests for prompt delegation orchestration."""

import pytest

from coherence_engine.core.delegation import PromptDelegationEngine


class TestDelegationDecision:
    def test_auto_delegates_when_threshold_exceeded(self):
        engine = PromptDelegationEngine(auto_word_threshold=10, auto_char_threshold=999999)
        prompt = " ".join(["token"] * 30)
        decision = engine.decide_delegation(prompt=prompt, auto_delegate=True)
        assert decision.delegated is True
        assert decision.target_agents >= 2

    def test_force_parallel_is_bounded(self):
        engine = PromptDelegationEngine()
        decision = engine.decide_delegation(prompt="short text", force_parallel=100)
        assert decision.target_agents == 4
        assert decision.delegated is True


class TestDelegationRun:
    def test_split_into_requested_chunks(self):
        engine = PromptDelegationEngine()
        prompt = (
            "Alpha block first sentence. Alpha block second sentence.\n\n"
            "Beta block first sentence. Beta block second sentence.\n\n"
            "Gamma block first sentence. Gamma block second sentence."
        )
        chunks = engine._split_prompt(prompt, target_chunks=3)
        assert len(chunks) == 3
        assert all(c.word_count > 0 for c in chunks)

    def test_run_force_parallel_and_selected_agents(self):
        engine = PromptDelegationEngine()
        prompt = (
            "First section has content and clear statements.\n\n"
            "Second section has different content and additional details.\n\n"
            "Third section adds more context and constraints."
        )
        result = engine.run(
            prompt=prompt,
            output_format="json",
            force_parallel=2,
            auto_delegate=False,
            selected_agents=["planner", "builder"],
        )
        assert result["parallel_agents_used"] == 2
        assert len(result["runs"]) == 2
        names = {r["agent"]["name"] for r in result["runs"]}
        assert names.issubset({"planner", "builder"})

    def test_run_rejects_unknown_selected_agents(self):
        engine = PromptDelegationEngine()
        with pytest.raises(ValueError):
            engine.run(
                prompt="Some prompt with enough words to be valid.",
                selected_agents=["does-not-exist"],
            )
