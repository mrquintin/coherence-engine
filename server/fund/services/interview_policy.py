"""Adaptive interview policy engine (prompt 41).

Replaces the linear topic walk in :mod:`voice_intake` with a
deterministic, rules-based policy over a directed acyclic topic
graph. Inputs: the live ``session_state`` (persisted on
``InterviewSession.state_json``) plus a ``last_answer_features``
record summarising the most recent answer (confidence, evidence
score, anti-gaming flags, duration). Output: the next
:class:`Question` to ask, or ``None`` when the coverage criterion
is met or the per-call duration cap has been reached.

Determinism
-----------

The policy MUST NOT make any LLM or network call. Given the same
``(session_state, last_answer_features)`` pair the result is
bit-identical — the test suite pins fixture walks against an
expected sequence. Cycle-free topic graphs are enforced at load
time so a malformed graph cannot loop the engine forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


__all__ = [
    "InterviewPolicyError",
    "TopicGraph",
    "TopicNode",
    "Question",
    "AnswerFeatures",
    "load_topic_graph",
    "init_state",
    "apply_answer",
    "next_question",
    "coverage_complete",
    "duration_exhausted",
]


_GRAPH_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "interview_topic_graph.json"
)

POLICY_VERSION = "1"


class InterviewPolicyError(RuntimeError):
    """Raised when the policy engine cannot proceed (bad graph or state)."""


# ---------------------------------------------------------------------------
# Graph data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicNode:
    id: str
    kind: str  # "primary" | "follow_up"
    priority: str  # "high" | "medium" | "low"
    prompt: str
    edges: Mapping[str, str]
    of: Optional[str] = None  # parent topic for follow_up nodes


@dataclass(frozen=True)
class TopicGraph:
    version: str
    duration_seconds_cap: int
    confidence_threshold: float
    anti_gaming_threshold: int
    max_follow_ups_per_topic: int
    start: str
    high_priority_topics: Tuple[str, ...]
    nodes: Mapping[str, TopicNode]


@dataclass(frozen=True)
class Question:
    topic_id: str
    prompt: str
    kind: str


@dataclass(frozen=True)
class AnswerFeatures:
    """Per-answer signal record handed to :func:`next_question`.

    Fields are deliberately small + deterministic. Upstream code
    (transcript-quality + scoring) computes them; the policy only
    consumes them.
    """

    topic_id: str
    confidence: float = 0.0
    evidence_score: float = 0.0
    anti_gaming_flagged: bool = False
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Loading + cycle validation
# ---------------------------------------------------------------------------


def _validate_acyclic(nodes: Mapping[str, TopicNode]) -> None:
    """Raise if ``nodes`` contains any directed cycle.

    Iterative DFS with three-colour marking — white (unseen), grey
    (on the current stack), black (finished). A grey-to-grey edge
    proves a cycle. Iterative form keeps recursion off the call
    stack so a pathological graph cannot crash the loader.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {nid: WHITE for nid in nodes}
    for root in nodes:
        if colour[root] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        order: list[str] = []
        colour[root] = GREY
        order.append(root)
        while stack:
            nid, idx = stack[-1]
            children = list(nodes[nid].edges.values())
            if idx < len(children):
                stack[-1] = (nid, idx + 1)
                child = children[idx]
                if child not in nodes:
                    raise InterviewPolicyError(
                        f"interview_policy_unknown_edge_target node={nid} target={child}"
                    )
                c = colour[child]
                if c == GREY:
                    raise InterviewPolicyError(
                        f"interview_policy_topic_graph_cycle "
                        f"from={nid} to={child}"
                    )
                if c == WHITE:
                    colour[child] = GREY
                    order.append(child)
                    stack.append((child, 0))
            else:
                colour[nid] = BLACK
                stack.pop()


def load_topic_graph(path: Optional[Path] = None) -> TopicGraph:
    src = path or _GRAPH_PATH
    with src.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    nodes_raw = raw.get("topics") or {}
    if not nodes_raw:
        raise InterviewPolicyError("interview_policy_topic_graph_empty")
    nodes: Dict[str, TopicNode] = {}
    for nid, body in nodes_raw.items():
        nodes[nid] = TopicNode(
            id=nid,
            kind=str(body.get("kind", "primary")),
            priority=str(body.get("priority", "medium")),
            prompt=str(body.get("prompt", "")),
            edges=dict(body.get("edges") or {}),
            of=body.get("of"),
        )
    start = str(raw.get("start") or "")
    if start not in nodes:
        raise InterviewPolicyError(
            f"interview_policy_invalid_start start={start!r}"
        )
    _validate_acyclic(nodes)
    high = tuple(raw.get("high_priority_topics") or [])
    for tid in high:
        if tid not in nodes:
            raise InterviewPolicyError(
                f"interview_policy_unknown_high_priority topic={tid}"
            )
    return TopicGraph(
        version=str(raw.get("version", "1")),
        duration_seconds_cap=int(raw.get("duration_seconds_cap", 1800)),
        confidence_threshold=float(raw.get("confidence_threshold", 0.6)),
        anti_gaming_threshold=int(raw.get("anti_gaming_threshold", 1)),
        max_follow_ups_per_topic=int(raw.get("max_follow_ups_per_topic", 1)),
        start=start,
        high_priority_topics=high,
        nodes=nodes,
    )


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
#
# ``state`` is a plain JSON-serialisable dict so it can round-trip
# through ``InterviewSession.state_json`` without any custom encoder.
# Schema:
#
#     {
#       "policy_version": "1",
#       "graph_version": "1",
#       "started_at": "<iso>",
#       "duration_seconds_used": 0.0,
#       "duration_seconds_cap": 1800,
#       "next_question": {"topic_id": "...", "prompt": "...", "kind": "..."} | null,
#       "topics": {
#         "<topic_id>": {
#            "asked": bool,
#            "answered": bool,
#            "confidence": float,
#            "evidence_score": float,
#            "anti_gaming_flagged": bool,
#            "follow_ups_asked": int
#         }
#       },
#       "anti_gaming_count": int,
#       "recovery_attempts": int,
#       "completed": bool
#     }


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def init_state(graph: TopicGraph) -> Dict[str, Any]:
    first = graph.nodes[graph.start]
    state: Dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "graph_version": graph.version,
        "started_at": _utc_now_iso(),
        "duration_seconds_used": 0.0,
        "duration_seconds_cap": int(graph.duration_seconds_cap),
        "next_question": {
            "topic_id": first.id,
            "prompt": first.prompt,
            "kind": first.kind,
        },
        "topics": {
            tid: {
                "asked": False,
                "answered": False,
                "confidence": 0.0,
                "evidence_score": 0.0,
                "anti_gaming_flagged": False,
                "follow_ups_asked": 0,
            }
            for tid in graph.nodes
        },
        "anti_gaming_count": 0,
        "recovery_attempts": 0,
        "completed": False,
    }
    return state


def _ensure_topic(state: Dict[str, Any], topic_id: str) -> Dict[str, Any]:
    topics = state.setdefault("topics", {})
    rec = topics.get(topic_id)
    if rec is None:
        rec = {
            "asked": False,
            "answered": False,
            "confidence": 0.0,
            "evidence_score": 0.0,
            "anti_gaming_flagged": False,
            "follow_ups_asked": 0,
        }
        topics[topic_id] = rec
    return rec


def apply_answer(
    state: Dict[str, Any],
    features: AnswerFeatures,
    *,
    graph: TopicGraph,
) -> None:
    """Fold ``features`` into ``state`` in-place.

    Updates the topic record, the running duration counter, and the
    anti-gaming counter. Idempotent on the same ``features`` only in
    the trivial sense — re-applying counts the duration twice; the
    caller owns dedupe.
    """
    rec = _ensure_topic(state, features.topic_id)
    rec["asked"] = True
    rec["answered"] = True
    rec["confidence"] = float(features.confidence)
    rec["evidence_score"] = float(features.evidence_score)
    rec["anti_gaming_flagged"] = bool(features.anti_gaming_flagged)
    state["duration_seconds_used"] = float(
        state.get("duration_seconds_used", 0.0)
    ) + float(features.duration_seconds)
    if features.anti_gaming_flagged:
        state["anti_gaming_count"] = int(state.get("anti_gaming_count", 0)) + 1
    # Bump the parent-topic follow-up counter when we just answered a
    # follow-up node; the policy uses this to enforce
    # max_follow_ups_per_topic.
    node = graph.nodes.get(features.topic_id)
    if node is not None and node.kind == "follow_up" and node.of:
        parent = _ensure_topic(state, node.of)
        parent["follow_ups_asked"] = int(parent.get("follow_ups_asked", 0)) + 1


# ---------------------------------------------------------------------------
# Coverage + duration
# ---------------------------------------------------------------------------


def coverage_complete(state: Mapping[str, Any], graph: TopicGraph) -> bool:
    threshold = graph.confidence_threshold
    topics = state.get("topics") or {}
    for tid in graph.high_priority_topics:
        rec = topics.get(tid) or {}
        if not rec.get("answered"):
            return False
        if float(rec.get("confidence", 0.0)) < threshold:
            return False
    return True


def duration_exhausted(state: Mapping[str, Any]) -> bool:
    cap = float(state.get("duration_seconds_cap", 0) or 0)
    used = float(state.get("duration_seconds_used", 0) or 0)
    return cap > 0 and used >= cap


# ---------------------------------------------------------------------------
# Edge selection
# ---------------------------------------------------------------------------


def _pick_edge(
    node: TopicNode,
    *,
    last: Optional[AnswerFeatures],
    state: Mapping[str, Any],
    graph: TopicGraph,
) -> Optional[str]:
    """Return the next topic-id according to ``node.edges`` and predicates.

    Predicate priority is fixed (no ambiguity):

    1. ``if_high_anti_gaming`` — the running anti-gaming counter for
       the session has met or exceeded ``graph.anti_gaming_threshold``.
       This skips deep follow-ups and steers the call toward
       lower-risk topics.
    2. ``if_low_evidence`` — the *just-answered* topic has an
       evidence score below the confidence threshold AND we have not
       yet exhausted the per-topic follow-up budget. This routes to
       a clarifying follow-up.
    3. ``default`` — straight progression.
    """
    edges = node.edges
    if not edges:
        return None
    anti_count = int(state.get("anti_gaming_count", 0) or 0)
    if (
        "if_high_anti_gaming" in edges
        and anti_count >= graph.anti_gaming_threshold
    ):
        return edges["if_high_anti_gaming"]
    if "if_low_evidence" in edges and last is not None:
        topics = state.get("topics") or {}
        rec = topics.get(node.id) or {}
        follow_ups = int(rec.get("follow_ups_asked", 0))
        evidence_low = float(last.evidence_score) < graph.confidence_threshold
        if (
            evidence_low
            and follow_ups < graph.max_follow_ups_per_topic
            # Anti-gaming flagged answers should NOT trigger a deep
            # follow-up — that is the whole point of the anti-gaming
            # branch. We check it again here so a node that has both
            # predicates and an anti-gaming hit still skips the dive.
            and not (last.anti_gaming_flagged)
        ):
            return edges["if_low_evidence"]
    return edges.get("default")


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def next_question(
    state: Dict[str, Any],
    last_answer_features: Optional[AnswerFeatures] = None,
    *,
    graph: Optional[TopicGraph] = None,
) -> Optional[Question]:
    """Decide the next interview question or terminate the call.

    Returns ``None`` when (a) the high-priority coverage criterion is
    met, or (b) the per-call duration cap has been reached, or (c)
    the topic graph offers no further edge from the current node.
    The returned :class:`Question` is ALSO written back to
    ``state["next_question"]`` so a recovery flow can resume from it
    after a dropped call.
    """
    g = graph or load_topic_graph()
    if state.get("completed"):
        return None
    if duration_exhausted(state):
        state["completed"] = True
        state["next_question"] = None
        return None
    if last_answer_features is not None:
        apply_answer(state, last_answer_features, graph=g)
    if coverage_complete(state, g):
        state["completed"] = True
        state["next_question"] = None
        return None

    # Determine the "current" node: the one whose answer just
    # arrived (if any) or the start of the graph for a fresh state.
    if last_answer_features is not None:
        current_id = last_answer_features.topic_id
    else:
        nq = state.get("next_question")
        current_id = (nq or {}).get("topic_id") or g.start
    current = g.nodes.get(current_id)
    if current is None:
        raise InterviewPolicyError(
            f"interview_policy_unknown_current_topic id={current_id!r}"
        )

    # Walk forward until we land on a topic that has not been asked,
    # or fall off the graph. Bounded by the number of nodes so a
    # bug in the graph cannot loop forever (the acyclic check at
    # load time is the primary guard, this is belt-and-braces).
    visited: set[str] = set()
    cursor: Optional[str] = current.id
    last = last_answer_features
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        node = g.nodes[cursor]
        topics = state.get("topics") or {}
        rec = topics.get(cursor) or {}
        if not rec.get("asked"):
            _ensure_topic(state, cursor)["asked"] = True
            q = Question(topic_id=node.id, prompt=node.prompt, kind=node.kind)
            state["next_question"] = {
                "topic_id": q.topic_id,
                "prompt": q.prompt,
                "kind": q.kind,
            }
            return q
        cursor = _pick_edge(node, last=last, state=state, graph=g)
        # After the first hop, the "last answer" no longer applies:
        # subsequent edge selections are progression-only.
        last = None

    state["completed"] = True
    state["next_question"] = None
    return None
