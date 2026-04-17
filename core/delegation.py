"""Prompt delegation orchestrator for large-task parallel analysis.

This module creates an internal "multi-subagent" workflow:
- auto-delegate very large prompts
- force split any prompt across N agents (up to 4)
- assign chunks to named agent profiles
- execute chunk analysis in parallel
- generate delegate-ready prompts and a synthesis prompt
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
import json
import math
import os
import re

from coherence_engine.config import EngineConfig
from coherence_engine.core.scorer import CoherenceScorer


MAX_AGENTS = 4
DEFAULT_AUTO_WORD_THRESHOLD = 1000
DEFAULT_AUTO_CHAR_THRESHOLD = 7000


@dataclass
class AgentProfile:
    name: str
    role: str
    objective: str


@dataclass
class PromptChunk:
    index: int
    text: str
    word_count: int


@dataclass
class DelegationDecision:
    delegated: bool
    reason: str
    target_agents: int


class PromptDelegationEngine:
    """Plan and execute multi-agent style prompt delegation."""

    def __init__(
        self,
        auto_word_threshold: int = DEFAULT_AUTO_WORD_THRESHOLD,
        auto_char_threshold: int = DEFAULT_AUTO_CHAR_THRESHOLD,
    ) -> None:
        self.auto_word_threshold = auto_word_threshold
        self.auto_char_threshold = auto_char_threshold
        self.default_profiles = [
            AgentProfile(
                name="planner",
                role="Task decomposition lead",
                objective="Break the task into concrete, non-overlapping subgoals.",
            ),
            AgentProfile(
                name="critic",
                role="Risk and contradiction hunter",
                objective="Find contradictions, risks, and missing assumptions.",
            ),
            AgentProfile(
                name="builder",
                role="Execution and implementation specialist",
                objective="Turn intent into direct implementation-ready instructions.",
            ),
            AgentProfile(
                name="synthesizer",
                role="Final synthesis lead",
                objective="Merge all outputs into a single complete plan of action.",
            ),
        ]

    def load_agent_profiles(self, path: Optional[str]) -> List[AgentProfile]:
        """Load custom agent profiles from JSON, else return defaults."""
        if not path:
            return list(self.default_profiles)
        if not os.path.isfile(path):
            raise ValueError(f"Agent list file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if not isinstance(raw, list):
            raise ValueError("Agent list file must contain a JSON array.")

        profiles: List[AgentProfile] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            role = str(entry.get("role", "")).strip()
            objective = str(entry.get("objective", "")).strip()
            if not name:
                continue
            profiles.append(
                AgentProfile(
                    name=name,
                    role=role or "General subagent",
                    objective=objective or "Process assigned chunk accurately and completely.",
                )
            )

        if not profiles:
            raise ValueError("No valid agent profiles found in agent list file.")
        if len(profiles) > MAX_AGENTS:
            profiles = profiles[:MAX_AGENTS]
        return profiles

    def decide_delegation(
        self,
        prompt: str,
        force_parallel: Optional[int] = None,
        auto_delegate: bool = True,
    ) -> DelegationDecision:
        """Determine if prompt should be delegated and to how many agents."""
        words = self._word_count(prompt)
        chars = len(prompt)

        if force_parallel is not None:
            n = self._bounded_agents(force_parallel)
            return DelegationDecision(
                delegated=n > 1,
                reason=f"forced_parallel={n}",
                target_agents=n,
            )

        if not auto_delegate:
            return DelegationDecision(delegated=False, reason="auto_delegate_disabled", target_agents=1)

        if words >= self.auto_word_threshold or chars >= self.auto_char_threshold:
            # Scale up agent count with task size, capped to 4.
            estimated = max(
                2,
                math.ceil(words / max(1, self.auto_word_threshold)),
                math.ceil(chars / max(1, self.auto_char_threshold)),
            )
            n = self._bounded_agents(estimated)
            return DelegationDecision(
                delegated=n > 1,
                reason=f"auto_threshold_exceeded(words={words},chars={chars})",
                target_agents=n,
            )

        return DelegationDecision(delegated=False, reason="below_auto_threshold", target_agents=1)

    def run(
        self,
        prompt: str,
        output_format: str = "text",
        force_parallel: Optional[int] = None,
        auto_delegate: bool = True,
        selected_agents: Optional[List[str]] = None,
        agent_list_file: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """Execute prompt delegation workflow and return aggregate output."""
        if not prompt.strip():
            raise ValueError("Prompt is empty.")

        profiles = self.load_agent_profiles(agent_list_file)
        if selected_agents:
            wanted = {name.strip() for name in selected_agents if name.strip()}
            profiles = [p for p in profiles if p.name in wanted]
            if not profiles:
                raise ValueError("No selected agents found in the available agent list.")

        decision = self.decide_delegation(
            prompt=prompt,
            force_parallel=force_parallel,
            auto_delegate=auto_delegate,
        )

        target_agents = min(decision.target_agents, len(profiles), MAX_AGENTS)
        chunks = self._split_prompt(prompt, target_agents)
        assigned = self._assign_agents(chunks, profiles[:target_agents])
        runs = self._execute_parallel(assigned, output_format=output_format, verbose=verbose)
        aggregate = self._aggregate_runs(runs, decision, output_format)
        return aggregate

    def _split_prompt(self, prompt: str, target_chunks: int) -> List[PromptChunk]:
        """Split prompt by paragraph and sentence boundaries."""
        if target_chunks <= 1:
            return [PromptChunk(index=1, text=prompt.strip(), word_count=self._word_count(prompt))]

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", prompt) if p.strip()]
        if len(paragraphs) < target_chunks:
            paragraphs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prompt) if s.strip()]
        if not paragraphs:
            paragraphs = [prompt.strip()]

        bins: List[List[str]] = [[] for _ in range(target_chunks)]
        bin_sizes = [0 for _ in range(target_chunks)]

        for segment in sorted(paragraphs, key=self._word_count, reverse=True):
            idx = bin_sizes.index(min(bin_sizes))
            bins[idx].append(segment)
            bin_sizes[idx] += self._word_count(segment)

        chunks: List[PromptChunk] = []
        chunk_index = 1
        for bucket in bins:
            text = "\n\n".join(bucket).strip()
            if not text:
                continue
            chunks.append(PromptChunk(index=chunk_index, text=text, word_count=self._word_count(text)))
            chunk_index += 1

        if not chunks:
            chunks = [PromptChunk(index=1, text=prompt.strip(), word_count=self._word_count(prompt))]

        return chunks

    def _assign_agents(
        self,
        chunks: List[PromptChunk],
        profiles: List[AgentProfile],
    ) -> List[Dict[str, Any]]:
        assigned: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            agent = profiles[i % len(profiles)]
            assigned.append({"agent": agent, "chunk": chunk})
        return assigned

    def _execute_parallel(
        self,
        assigned: List[Dict[str, Any]],
        output_format: str,
        verbose: bool,
    ) -> List[Dict[str, Any]]:
        if len(assigned) == 1:
            pair = assigned[0]
            return [self._run_chunk(pair["agent"], pair["chunk"], output_format=output_format, verbose=verbose)]

        with ThreadPoolExecutor(max_workers=min(len(assigned), MAX_AGENTS)) as executor:
            futures = [
                executor.submit(
                    self._run_chunk,
                    pair["agent"],
                    pair["chunk"],
                    output_format,
                    verbose,
                )
                for pair in assigned
            ]
            return [f.result() for f in futures]

    def _run_chunk(
        self,
        agent: AgentProfile,
        chunk: PromptChunk,
        output_format: str,
        verbose: bool,
    ) -> Dict[str, Any]:
        config = EngineConfig(output_format=output_format, verbose=verbose)
        scorer = CoherenceScorer(config)
        result = scorer.score(chunk.text)

        delegate_prompt = self._delegate_prompt_for(agent, chunk)
        return {
            "agent": asdict(agent),
            "chunk_index": chunk.index,
            "chunk_word_count": chunk.word_count,
            "delegate_prompt": delegate_prompt,
            "report": result.report(fmt=output_format),
            "score": round(result.composite_score, 4),
            "warnings": [w for lr in result.layer_results for w in lr.warnings],
            "metadata": result.metadata,
        }

    def _aggregate_runs(
        self,
        runs: List[Dict[str, Any]],
        decision: DelegationDecision,
        output_format: str,
    ) -> Dict[str, Any]:
        total_words = sum(max(1, r["chunk_word_count"]) for r in runs)
        weighted_score = sum(r["score"] * max(1, r["chunk_word_count"]) for r in runs) / max(1, total_words)
        synthesis_prompt = self._build_synthesis_prompt(runs)

        return {
            "delegation": asdict(decision),
            "parallel_agents_used": len(runs),
            "aggregate_score": round(weighted_score, 4),
            "output_format": output_format,
            "runs": sorted(runs, key=lambda r: r["chunk_index"]),
            "synthesis_prompt": synthesis_prompt,
        }

    def _delegate_prompt_for(self, agent: AgentProfile, chunk: PromptChunk) -> str:
        return (
            f"You are subagent '{agent.name}'.\n"
            f"Role: {agent.role}\n"
            f"Objective: {agent.objective}\n"
            f"Chunk: {chunk.index} ({chunk.word_count} words)\n\n"
            "Task:\n"
            "1) Analyze the assigned chunk thoroughly.\n"
            "2) Identify key claims, risks, dependencies, and ambiguities.\n"
            "3) Output concrete actions and suggested implementation sequence.\n\n"
            f"Assigned Prompt Chunk:\n{chunk.text}"
        )

    def _build_synthesis_prompt(self, runs: List[Dict[str, Any]]) -> str:
        sections = []
        for run in sorted(runs, key=lambda r: r["chunk_index"]):
            sections.append(
                f"[Chunk {run['chunk_index']} | Agent {run['agent']['name']} | "
                f"Score {run['score']}]"
            )
        section_text = "\n".join(sections)
        return (
            "You are the synthesis lead. Merge delegate outputs into one complete execution plan.\n"
            "Requirements:\n"
            "- remove overlaps\n"
            "- resolve contradictions\n"
            "- ensure no missing dependencies\n"
            "- produce one ordered implementation plan with acceptance criteria\n\n"
            "Delegate Context:\n"
            f"{section_text}"
        )

    @staticmethod
    def _word_count(text: str) -> int:
        return len(text.split()) if text else 0

    @staticmethod
    def _bounded_agents(n: int) -> int:
        return max(1, min(MAX_AGENTS, int(n)))
