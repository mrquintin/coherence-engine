"""Partner / admin decision-override service (prompt 35).

Encapsulates the write-side of the partner-dashboard override flow.
Keeps validation, idempotency, RBAC, and audit / event emission in one
place so the router (``server.fund.routers.partner_api``) and any
future CLI / admin path stay thin.

Contract summary:

* RBAC — only principals carrying role ``partner`` or ``admin`` may
  override. The router defers to ``require_role`` (re-exported here as
  a convenience), which itself wraps :func:`enforce_roles`.
* ``reason_code`` must be one of the four allow-listed enum values:
  ``factual_error``, ``policy_misalignment``, ``regulatory_constraint``,
  ``manual_diligence``.
* ``reason_text`` must be at least 40 characters of meaningful prose.
* Overriding a ``pass`` verdict to ``reject`` requires a non-empty
  ``justification_uri`` pointing at a memo (signed-URL pattern shared
  with the rest of the fund stack).
* The same application cannot be overridden twice in active state. A
  second call without ``unrevise=True`` returns the prior override row
  unchanged (idempotent). A call with ``unrevise=True`` marks the
  prior row ``superseded`` and writes a fresh ``active`` row.
* Every successful write emits a ``decision_overridden.v1`` outbox
  event so the founder-notification + portfolio-projection consumers
  pick up the new verdict.

The service is intentionally synchronous and database-bound; it never
issues HTTP. The router is responsible for serializing the result,
mapping ``OverrideError`` exceptions to error envelopes, and stamping
the audit log via :func:`audit_log`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.security import enforce_roles
from coherence_engine.server.fund.services.event_publisher import EventPublisher


_LOG = logging.getLogger(__name__)


# Re-export under the prompt-mandated name. Routers call
# ``require_role(request, ("partner","admin"))`` rather than the
# slightly-different middleware spelling so the partner-side surface
# reads as a single coherent unit.
def require_role(request, allowed_roles: Tuple[str, ...]):
    """Thin wrapper around :func:`enforce_roles` (prompt 35).

    Returns ``None`` on success, or a :class:`fastapi.responses.JSONResponse`
    with status 403 and the standard error envelope when the principal
    role is not in ``allowed_roles``.
    """

    return enforce_roles(request, allowed_roles)


VALID_REASON_CODES = frozenset(
    {
        "factual_error",
        "policy_misalignment",
        "regulatory_constraint",
        "manual_diligence",
    }
)

VALID_OVERRIDE_VERDICTS = frozenset({"pass", "reject", "manual_review"})

MIN_REASON_TEXT_LENGTH = 40

OVERRIDE_EVENT_TYPE = "decision_overridden"


class OverrideError(ValueError):
    """Raised on validation / state errors during override write.

    The ``code`` attribute is a short stable string the router maps
    onto the JSON error envelope ``error.code`` field.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class OverrideResult:
    """Return type of :meth:`DecisionOverrideService.create_override`."""

    override: models.DecisionOverride
    created: bool  # True when a new row was written; False on idempotent reuse
    superseded_id: Optional[str] = None


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class DecisionOverrideService:
    """Application-layer policy + persistence for decision overrides."""

    def __init__(self, db: Session, publisher: Optional[EventPublisher] = None):
        self.db = db
        self.publisher = publisher or EventPublisher(db)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_reason_code(reason_code: str) -> None:
        if reason_code not in VALID_REASON_CODES:
            raise OverrideError(
                "INVALID_REASON_CODE",
                f"reason_code must be one of {sorted(VALID_REASON_CODES)}",
            )

    @staticmethod
    def _validate_reason_text(reason_text: str) -> None:
        if not isinstance(reason_text, str):
            raise OverrideError(
                "INVALID_REASON_TEXT", "reason_text must be a string"
            )
        cleaned = reason_text.strip()
        if len(cleaned) < MIN_REASON_TEXT_LENGTH:
            raise OverrideError(
                "REASON_TEXT_TOO_SHORT",
                f"reason_text must be at least {MIN_REASON_TEXT_LENGTH} characters",
            )

    @staticmethod
    def _validate_verdict(verdict: str) -> None:
        if verdict not in VALID_OVERRIDE_VERDICTS:
            raise OverrideError(
                "INVALID_VERDICT",
                f"override_verdict must be one of {sorted(VALID_OVERRIDE_VERDICTS)}",
            )

    @staticmethod
    def _validate_pass_to_reject(
        original_verdict: str,
        override_verdict: str,
        justification_uri: Optional[str],
    ) -> None:
        if (
            original_verdict == "pass"
            and override_verdict == "reject"
            and not (justification_uri and justification_uri.strip())
        ):
            raise OverrideError(
                "MEMO_REQUIRED",
                "overriding pass→reject requires justification_uri (memo)",
            )

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _load_active_override(
        self, application_id: str
    ) -> Optional[models.DecisionOverride]:
        return (
            self.db.query(models.DecisionOverride)
            .filter(models.DecisionOverride.application_id == application_id)
            .filter(models.DecisionOverride.status == "active")
            .order_by(models.DecisionOverride.overridden_at.desc())
            .first()
        )

    def _load_decision(self, application_id: str) -> Optional[models.Decision]:
        return (
            self.db.query(models.Decision)
            .filter(models.Decision.application_id == application_id)
            .one_or_none()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_override(
        self,
        *,
        application_id: str,
        override_verdict: str,
        reason_code: str,
        reason_text: str,
        overridden_by: str,
        justification_uri: Optional[str] = None,
        unrevise: bool = False,
        trace_id: Optional[str] = None,
    ) -> OverrideResult:
        """Persist a new override row (or reuse an existing one).

        Validation order matters: input sanity → existence checks →
        cross-field business rules → DB write → event emission. The
        router catches :class:`OverrideError` and serializes
        ``code`` + ``message`` into the error envelope.
        """

        self._validate_verdict(override_verdict)
        self._validate_reason_code(reason_code)
        self._validate_reason_text(reason_text)

        if not overridden_by or not str(overridden_by).strip():
            raise OverrideError(
                "MISSING_ACTOR", "overridden_by is required"
            )

        decision = self._load_decision(application_id)
        if decision is None:
            raise OverrideError(
                "DECISION_NOT_FOUND",
                f"no decision exists for application {application_id}",
            )
        original_verdict = decision.decision

        self._validate_pass_to_reject(
            original_verdict, override_verdict, justification_uri
        )

        existing = self._load_active_override(application_id)
        superseded_id: Optional[str] = None
        if existing is not None:
            if not unrevise:
                # Idempotent: same caller hitting the endpoint twice
                # with the same intent → return the prior row. We do
                # NOT re-emit an event in this case.
                return OverrideResult(
                    override=existing, created=False, superseded_id=None
                )
            existing.status = "superseded"
            self.db.flush()
            superseded_id = existing.id

        override_row = models.DecisionOverride(
            id=f"do_{uuid.uuid4().hex[:24]}",
            application_id=application_id,
            original_verdict=original_verdict,
            override_verdict=override_verdict,
            reason_code=reason_code,
            reason_text=reason_text.strip(),
            overridden_by=overridden_by.strip(),
            justification_uri=(justification_uri or "").strip(),
            status="active",
            overridden_at=_utc_now(),
        )
        self.db.add(override_row)
        self.db.flush()

        self._emit_event(
            override_row,
            superseded_id=superseded_id,
            trace_id=trace_id,
        )

        return OverrideResult(
            override=override_row,
            created=True,
            superseded_id=superseded_id,
        )

    # ------------------------------------------------------------------
    # Read-side helpers (used by /partner/pipeline)
    # ------------------------------------------------------------------

    def list_active_overrides_for(
        self, application_ids: Tuple[str, ...]
    ) -> Dict[str, models.DecisionOverride]:
        if not application_ids:
            return {}
        rows = (
            self.db.query(models.DecisionOverride)
            .filter(
                models.DecisionOverride.application_id.in_(list(application_ids))
            )
            .filter(models.DecisionOverride.status == "active")
            .all()
        )
        out: Dict[str, models.DecisionOverride] = {}
        for r in rows:
            prior = out.get(r.application_id)
            if prior is None or (
                r.overridden_at and prior.overridden_at
                and r.overridden_at > prior.overridden_at
            ):
                out[r.application_id] = r
        return out

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        override_row: models.DecisionOverride,
        *,
        superseded_id: Optional[str],
        trace_id: Optional[str],
    ) -> None:
        payload: Dict[str, Any] = {
            "override_id": override_row.id,
            "application_id": override_row.application_id,
            "original_verdict": override_row.original_verdict,
            "override_verdict": override_row.override_verdict,
            "reason_code": override_row.reason_code,
            "overridden_by": override_row.overridden_by,
            "justification_uri": override_row.justification_uri or "",
            "superseded_override_id": superseded_id or "",
            "overridden_at": (
                override_row.overridden_at.isoformat().replace("+00:00", "Z")
                if override_row.overridden_at
                else ""
            ),
        }
        try:
            self.publisher.publish(
                event_type=OVERRIDE_EVENT_TYPE,
                producer="partner_dashboard",
                trace_id=trace_id or str(uuid.uuid4()),
                idempotency_key=override_row.id,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Event emission failure must not silently swallow the
            # write — surface it so the caller can decide whether to
            # retry. We re-raise so the SQLAlchemy transaction rolls
            # back at the router boundary.
            _LOG.error(
                "decision_overridden_event_publish_failed override_id=%s err=%s",
                override_row.id,
                exc,
            )
            raise


__all__ = [
    "DecisionOverrideService",
    "OverrideError",
    "OverrideResult",
    "VALID_OVERRIDE_VERDICTS",
    "VALID_REASON_CODES",
    "MIN_REASON_TEXT_LENGTH",
    "OVERRIDE_EVENT_TYPE",
    "require_role",
]
