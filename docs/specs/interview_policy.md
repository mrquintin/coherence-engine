# Adaptive interview policy + interruption handling

Wave 11 / prompt 41. Replaces the linear topic walk in
`server/fund/services/voice_intake.py` (prompts 38–39) with a
deterministic, rules-based policy over a directed acyclic
**topic graph**, and adds dropped-call recovery with a 24-hour
window.

## Goals

* Adaptive question selection: skip cheap topics when coverage is
  already high, dive on weak evidence with a clarifying follow-up,
  and steer past deep follow-ups when anti-gaming flags fire.
* Hard cap on call duration: when reached, the policy returns
  `None` and the session terminates.
* Resumability: persist enough state per session that a dropped
  call can be resumed within 24h from the same next-question.
* Determinism: the policy is pure rules over numbers and bools —
  no LLM calls, no random choices, no clock dependencies in the
  question-selection path. Fixture transcripts produce a pinned
  question sequence.

## Components

| Path | Role |
| --- | --- |
| `server/fund/data/interview_topic_graph.json` | Topic nodes + edge predicates |
| `server/fund/services/interview_policy.py` | Deterministic engine: state + `next_question` |
| `server/fund/services/interview_recovery.py` | 24h dropped-call recovery |
| `server/fund/services/voice_intake.py` | Wires `next_question_for_session` over `InterviewSession.state_json` |
| `alembic/versions/20260425_000008_interview_session_state.py` | Adds `fund_interview_sessions.state_json` |

## Topic graph

The graph file is loaded once and validated at load time:

* Every edge target must resolve to a known node.
* The graph must be **acyclic** (iterative three-colour DFS).
* The `start` topic must exist.

Each node carries `kind` (`primary` | `follow_up`), `priority`
(`high` | `medium` | `low`), a `prompt` (the spoken line), and an
`edges` map keyed by predicate:

| Predicate | Fires when |
| --- | --- |
| `if_high_anti_gaming` | running anti-gaming counter ≥ `anti_gaming_threshold` |
| `if_low_evidence` | the just-answered topic's evidence score < `confidence_threshold` AND the parent has not exhausted `max_follow_ups_per_topic` AND the answer was NOT anti-gaming-flagged |
| `default` | straight progression |

Predicate priority is fixed: anti-gaming first, then low-evidence,
then default. The "anti-gaming overrides low-evidence" rule is the
reason a deep follow-up is suppressed when the founder is suspected
of gaming the questions — burning the call on a follow-up dive into
a flagged answer is exactly the failure mode we want to avoid.

## Coverage

A session is considered **covered** when every topic listed in
`high_priority_topics` has been answered with `confidence >=
confidence_threshold`. When coverage is met (or the duration cap
is reached) `next_question` returns `None`, sets `state.completed
= True`, and clears `state.next_question`.

## Session state

The state is a JSON-serialisable dict written to
`InterviewSession.state_json`:

```jsonc
{
  "policy_version": "1",
  "graph_version": "1",
  "started_at": "...",
  "duration_seconds_used": 0.0,
  "duration_seconds_cap": 1800,
  "next_question": {"topic_id": "...", "prompt": "...", "kind": "..."},
  "topics": {
    "<topic_id>": {
      "asked": false, "answered": false,
      "confidence": 0.0, "evidence_score": 0.0,
      "anti_gaming_flagged": false, "follow_ups_asked": 0
    }
  },
  "anti_gaming_count": 0,
  "recovery_attempts": 0,
  "completed": false
}
```

## Recovery (24h dropped-call window)

`interview_recovery.recover_session` is wired into the Twilio
`call_status=completed` webhook. Eligibility:

* `session.status != "completed"` AND `state.completed` is falsy.
* `state.recovery_attempts < 1` (at most one recovery per session).
* `session.created_at >= now - 24h`.
* Coverage is incomplete.

On eligible sessions the recovery flow:

1. Notifies the founder by email (cross-link to the prompt 14
   notification stack — a callable is injected by the webhook
   handler so unit tests can drive the flow without SMTP).
2. Reissues the outbound call with `state.next_question` as the
   resume point.
3. Bumps `recovery_attempts`.

A second recovery attempt raises `RecoveryRefused` —
`recovery_attempts > 1` is not permitted by contract.

## Determinism + replay

The policy makes no LLM call, no network call, and no clock-based
branching. Given identical `(state, last_answer_features)` pairs
the result is bit-identical, which is what the fixture-walk test
in `tests/test_interview_policy.py` pins. Replay from a stored
state file always selects the same next question.
