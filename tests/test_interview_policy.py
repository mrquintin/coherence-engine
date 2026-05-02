"""Tests for the deterministic adaptive interview policy (prompt 41)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coherence_engine.server.fund.services import interview_policy as policy


@pytest.fixture
def graph() -> policy.TopicGraph:
    return policy.load_topic_graph()


def test_topic_graph_loads_and_is_acyclic(graph: policy.TopicGraph):
    assert graph.start in graph.nodes
    # Every edge target resolves and no cycle exists — the loader
    # would have raised otherwise.
    for node in graph.nodes.values():
        for tgt in node.edges.values():
            assert tgt in graph.nodes


def test_topic_graph_rejects_cycle(tmp_path: Path):
    bad = {
        "version": "x",
        "duration_seconds_cap": 60,
        "confidence_threshold": 0.5,
        "anti_gaming_threshold": 1,
        "max_follow_ups_per_topic": 1,
        "start": "a",
        "high_priority_topics": [],
        "topics": {
            "a": {"kind": "primary", "priority": "high", "prompt": "a", "edges": {"default": "b"}},
            "b": {"kind": "primary", "priority": "high", "prompt": "b", "edges": {"default": "a"}},
        },
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(policy.InterviewPolicyError):
        policy.load_topic_graph(p)


def test_topic_graph_rejects_unknown_edge_target(tmp_path: Path):
    bad = {
        "version": "x",
        "duration_seconds_cap": 60,
        "confidence_threshold": 0.5,
        "anti_gaming_threshold": 1,
        "max_follow_ups_per_topic": 1,
        "start": "a",
        "high_priority_topics": [],
        "topics": {
            "a": {"kind": "primary", "priority": "high", "prompt": "a", "edges": {"default": "ghost"}},
        },
    }
    p = tmp_path / "ghost.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(policy.InterviewPolicyError):
        policy.load_topic_graph(p)


def test_init_state_seeds_next_question_with_start(graph: policy.TopicGraph):
    state = policy.init_state(graph)
    assert state["next_question"]["topic_id"] == graph.start
    # Start topic is NOT yet asked — the first ``next_question`` call
    # is what flips ``asked`` so a freshly-initialised state can be
    # asked the start question on the first hop.
    assert state["topics"][graph.start]["asked"] is False
    assert state["completed"] is False
    assert state["recovery_attempts"] == 0


def test_first_call_returns_start_question(graph: policy.TopicGraph):
    state = policy.init_state(graph)
    q = policy.next_question(state, None, graph=graph)
    assert q is not None
    assert q.topic_id == graph.start


def test_high_evidence_walk_skips_followups(graph: policy.TopicGraph):
    """A confident, evidence-rich answer to ``problem`` should jump to
    ``solution_mechanism`` rather than the ``problem_followup``."""
    state = policy.init_state(graph)
    # Simulate the founder having just answered the start topic well.
    features = policy.AnswerFeatures(
        topic_id=graph.start,
        confidence=0.9,
        evidence_score=0.9,
        anti_gaming_flagged=False,
        duration_seconds=30.0,
    )
    q = policy.next_question(state, features, graph=graph)
    assert q is not None
    assert q.topic_id == "solution_mechanism"


def test_low_evidence_routes_to_followup(graph: policy.TopicGraph):
    state = policy.init_state(graph)
    features = policy.AnswerFeatures(
        topic_id="problem",
        confidence=0.7,
        evidence_score=0.1,  # below threshold → trigger follow-up
        anti_gaming_flagged=False,
        duration_seconds=20.0,
    )
    q = policy.next_question(state, features, graph=graph)
    assert q is not None
    assert q.topic_id == "problem_followup"
    # The follow-up node bumps the parent counter on completion;
    # answer it to verify max_follow_ups guards the next round.
    fu_features = policy.AnswerFeatures(
        topic_id="problem_followup",
        confidence=0.7,
        evidence_score=0.2,
        anti_gaming_flagged=False,
        duration_seconds=15.0,
    )
    q2 = policy.next_question(state, fu_features, graph=graph)
    assert q2 is not None
    assert q2.topic_id == "solution_mechanism"


def test_anti_gaming_skips_deep_followups(graph: policy.TopicGraph):
    """Once anti-gaming has fired enough times, the policy must
    steer past deep follow-ups even if evidence is still low."""
    state = policy.init_state(graph)
    # Trip anti-gaming on the first answer.
    features = policy.AnswerFeatures(
        topic_id="problem",
        confidence=0.7,
        evidence_score=0.9,
        anti_gaming_flagged=True,
        duration_seconds=10.0,
    )
    policy.next_question(state, features, graph=graph)
    assert state["anti_gaming_count"] >= graph.anti_gaming_threshold
    # When we now answer ``evidence`` low, the if_high_anti_gaming
    # branch must override the if_low_evidence branch — landing on
    # ``execution`` rather than ``evidence_followup`` or ``moat``.
    # First force the engine onto ``evidence``.
    state["topics"]["solution_mechanism"]["asked"] = True
    state["topics"]["solution_mechanism"]["answered"] = True
    state["topics"]["solution_mechanism"]["confidence"] = 0.9
    ev_features = policy.AnswerFeatures(
        topic_id="evidence",
        confidence=0.5,
        evidence_score=0.1,
        anti_gaming_flagged=False,
        duration_seconds=10.0,
    )
    q = policy.next_question(state, ev_features, graph=graph)
    assert q is not None
    assert q.topic_id == "execution"


def test_duration_cap_terminates(graph: policy.TopicGraph):
    state = policy.init_state(graph)
    state["duration_seconds_used"] = float(graph.duration_seconds_cap)
    q = policy.next_question(state, None, graph=graph)
    assert q is None
    assert state["completed"] is True
    assert state["next_question"] is None


def test_coverage_complete_terminates(graph: policy.TopicGraph):
    state = policy.init_state(graph)
    for tid in graph.high_priority_topics:
        rec = state["topics"].setdefault(tid, {})
        rec["asked"] = True
        rec["answered"] = True
        rec["confidence"] = 0.95
        rec["evidence_score"] = 0.95
    q = policy.next_question(state, None, graph=graph)
    assert q is None
    assert state["completed"] is True


def test_policy_is_deterministic(graph: policy.TopicGraph):
    """Same (state, features) must return the same Question every time."""
    s1 = policy.init_state(graph)
    s2 = policy.init_state(graph)
    # Snap a fixed timestamp so the init_state diff is ignorable.
    s1["started_at"] = "2026-04-25T00:00:00+00:00"
    s2["started_at"] = "2026-04-25T00:00:00+00:00"
    feats = policy.AnswerFeatures(
        topic_id="problem", confidence=0.8, evidence_score=0.8, duration_seconds=20.0
    )
    q1 = policy.next_question(s1, feats, graph=graph)
    q2 = policy.next_question(s2, feats, graph=graph)
    assert q1 == q2


def test_full_walk_fixture_sequence(graph: policy.TopicGraph):
    """Pin the expected walk for the canonical 'high-confidence' transcript."""
    state = policy.init_state(graph)
    seen = [state["next_question"]["topic_id"]]
    # Every primary topic answered with high confidence/evidence.
    for tid in [
        "problem",
        "solution_mechanism",
        "evidence",
        "moat",
        "execution",
        "self_critique",
    ]:
        feats = policy.AnswerFeatures(
            topic_id=tid,
            confidence=0.9,
            evidence_score=0.9,
            anti_gaming_flagged=False,
            duration_seconds=30.0,
        )
        q = policy.next_question(state, feats, graph=graph)
        if q is not None:
            seen.append(q.topic_id)
    # We expect to walk start → solution → evidence → moat → execution
    # → self_critique, then terminate (None).
    assert seen[:6] == [
        "problem",
        "solution_mechanism",
        "evidence",
        "moat",
        "execution",
        "self_critique",
    ]


def test_apply_answer_increments_followup_counter(graph: policy.TopicGraph):
    state = policy.init_state(graph)
    fu_features = policy.AnswerFeatures(
        topic_id="problem_followup",
        confidence=0.7,
        evidence_score=0.7,
        duration_seconds=10.0,
    )
    policy.apply_answer(state, fu_features, graph=graph)
    assert state["topics"]["problem"]["follow_ups_asked"] == 1
