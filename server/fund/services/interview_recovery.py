"""Dropped-call recovery for the adaptive interview policy (prompt 41).

Pairs with :mod:`interview_policy`. When Twilio reports
``call_status=completed`` against an :class:`InterviewSession` whose
policy state is NOT ``completed`` (high-priority coverage unmet) we:

1. Notify the founder by email (cross-link to prompt 14).
2. Reissue an outbound call that resumes from
   ``state.next_question``.
3. Bump ``state.recovery_attempts``.

Each session may be recovered AT MOST ONCE — a second attempt is
refused (``RecoveryRefused``). Recoveries are also refused outside
the 24-hour window since the last activity timestamp.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services import interview_policy


__all__ = [
    "RecoveryRefused",
    "RecoveryResult",
    "should_recover",
    "recover_session",
    "resume_question",
]


_LOG = logging.getLogger("coherence_engine.fund.interview_recovery")

_RECOVERY_WINDOW_HOURS = 24
_MAX_RECOVERY_ATTEMPTS = 1


class RecoveryRefused(RuntimeError):
    """Raised when a recovery cannot proceed (already attempted, expired, etc)."""


@dataclass(frozen=True)
class RecoveryResult:
    session_id: str
    resumed_topic_id: str
    notification_sent: bool
    recovery_attempts: int


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_state(session: models.InterviewSession) -> Dict[str, Any]:
    raw = getattr(session, "state_json", None) or ""
    if not raw:
        return {}
    try:
        return dict(json.loads(raw))
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused(
            f"interview_recovery_state_unparseable session_id={session.id} error={exc!r}"
        ) from exc


def _save_state(session: models.InterviewSession, state: Mapping[str, Any]) -> None:
    session.state_json = json.dumps(state, sort_keys=True)


def resume_question(
    session: models.InterviewSession,
) -> Optional[interview_policy.Question]:
    """Return the persisted ``next_question`` from ``state_json``."""
    state = _load_state(session)
    nq = state.get("next_question")
    if not nq:
        return None
    return interview_policy.Question(
        topic_id=str(nq.get("topic_id") or ""),
        prompt=str(nq.get("prompt") or ""),
        kind=str(nq.get("kind") or "primary"),
    )


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def should_recover(
    session: models.InterviewSession,
    *,
    now: Optional[datetime] = None,
    graph: Optional[interview_policy.TopicGraph] = None,
) -> bool:
    """Return True when the session is eligible for an automatic recovery.

    Eligibility:

    * ``session.status`` is not ``completed``.
    * ``state.completed`` is falsy AND coverage is incomplete.
    * ``recovery_attempts`` is below the cap.
    * The session was created within the recovery window
      (24h by default).
    """
    if session.status == "completed":
        return False
    state = _load_state(session)
    if state.get("completed"):
        return False
    g = graph or interview_policy.load_topic_graph()
    if interview_policy.coverage_complete(state, g):
        return False
    if int(state.get("recovery_attempts", 0)) >= _MAX_RECOVERY_ATTEMPTS:
        return False
    started_at = session.created_at
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(
        hours=_RECOVERY_WINDOW_HOURS
    )
    if started_at < cutoff:
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recover_session(
    db: Session,
    session: models.InterviewSession,
    *,
    notifier: Optional[Callable[[models.InterviewSession, interview_policy.Question], None]] = None,
    redialer: Optional[Callable[[models.InterviewSession, interview_policy.Question], str]] = None,
    now: Optional[datetime] = None,
    graph: Optional[interview_policy.TopicGraph] = None,
) -> RecoveryResult:
    """Resume ``session`` from its persisted ``next_question``.

    The caller injects ``notifier`` (an email-sending callable) and
    ``redialer`` (the outbound-call placer); both default to no-op
    implementations so unit tests can drive the flow without touching
    Twilio or SMTP. In production these are wired by the webhook
    handler that observes ``call_status=completed``.

    Raises :class:`RecoveryRefused` when:

    * ``recovery_attempts`` is already ``>= 1`` (no double-recovery).
    * The recovery window (24h) has elapsed.
    * Coverage is already complete (nothing to resume).
    * No persisted ``next_question`` is available.
    """
    if not should_recover(session, now=now, graph=graph):
        # Distinguish the "already attempted" reason because operators
        # care about it and so do tests.
        state_peek = _load_state(session)
        if int(state_peek.get("recovery_attempts", 0)) >= _MAX_RECOVERY_ATTEMPTS:
            raise RecoveryRefused(
                f"interview_recovery_already_attempted session_id={session.id}"
            )
        raise RecoveryRefused(
            f"interview_recovery_not_eligible session_id={session.id}"
        )

    state = _load_state(session)
    nq = state.get("next_question")
    if not nq:
        raise RecoveryRefused(
            f"interview_recovery_no_next_question session_id={session.id}"
        )
    question = interview_policy.Question(
        topic_id=str(nq.get("topic_id") or ""),
        prompt=str(nq.get("prompt") or ""),
        kind=str(nq.get("kind") or "primary"),
    )

    notification_sent = False
    if notifier is not None:
        try:
            notifier(session, question)
            notification_sent = True
        except Exception:
            # The notification is a courtesy; the redial is the
            # actual recovery. Log + continue rather than abort the
            # whole resume because of an SMTP hiccup.
            _LOG.exception(
                "interview_recovery_notify_failed session_id=%s", session.id
            )

    if redialer is not None:
        try:
            redialer(session, question)
        except Exception:
            _LOG.exception(
                "interview_recovery_redial_failed session_id=%s", session.id
            )
            raise

    state["recovery_attempts"] = int(state.get("recovery_attempts", 0)) + 1
    # The session row stays ``active`` — the recovered call will
    # write further state updates as answers arrive.
    session.status = "active"
    _save_state(session, state)
    db.flush()

    _LOG.info(
        "interview_recovery_resumed session_id=%s topic=%s attempts=%d",
        session.id,
        question.topic_id,
        state["recovery_attempts"],
    )
    return RecoveryResult(
        session_id=session.id,
        resumed_topic_id=question.topic_id,
        notification_sent=notification_sent,
        recovery_attempts=int(state["recovery_attempts"]),
    )
