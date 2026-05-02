"""Capital deployment service (prompt 51).

The :class:`CapitalDeployment` interface owns the prepare / approve /
execute lifecycle of an :class:`InvestmentInstruction`. Backends
(Stripe Connect, Bank transfer) are pluggable via the
:mod:`capital_backends` module; this layer is the single gatekeeper
for the non-autonomy invariant:

* ``prepare`` is the only entry point for new instructions. Repeated
  prepares with the same ``idempotency_key`` collapse onto one row.
* ``approve`` writes a :class:`TreasurerApproval` row but does NOT
  call the backend.
* ``execute`` enforces the dual-approval rule for amounts above
  :data:`DUAL_APPROVAL_THRESHOLD_USD` and only then dispatches to the
  backend. The status transitions to ``sent`` on success, ``failed``
  on backend error.

Events
------

* ``investment_funding_prepared.v1`` is emitted on a successful
  ``prepare`` -- the outbox dispatcher then notifies treasurer queues.
* ``investment_funded.v1`` is emitted on a successful ``execute``
  (post-backend acknowledgement). A subsequent provider webhook may
  upgrade the instruction's ``error_code`` field on terminal failure
  but does not re-emit the event.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.capital_backends import (
    CapitalBackend,
    CapitalBackendError,
)
from coherence_engine.server.fund.services.event_publisher import EventPublisher


__all__ = [
    "CapitalDeployment",
    "CapitalDeploymentError",
    "InstructionStateError",
    "DUAL_APPROVAL_THRESHOLD_USD",
    "ALLOWED_METHODS",
    "ALLOWED_STATUSES",
    "compute_idempotency_key",
]


_LOG = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# Default $250k -- amounts at or above this require two distinct
# treasurer approvals before ``execute``. Operators can override via
# the ``CAPITAL_DUAL_APPROVAL_THRESHOLD_USD`` env var.
DUAL_APPROVAL_THRESHOLD_USD = int(
    os.environ.get("CAPITAL_DUAL_APPROVAL_THRESHOLD_USD", "250000")
)

ALLOWED_METHODS = frozenset({"stripe", "bank_transfer"})
ALLOWED_STATUSES = frozenset(
    {"prepared", "approved", "sent", "failed", "cancelled"}
)


class CapitalDeploymentError(Exception):
    """Raised by :class:`CapitalDeployment` for non-recoverable failures."""


class InstructionStateError(CapitalDeploymentError):
    """Raised on illegal state transitions (e.g. execute without approve)."""


def compute_idempotency_key(
    application_id: str,
    method: str,
    salt: str,
) -> str:
    """Deterministic idempotency key for a prepare attempt.

    The salt is typically a caller-provided request id so retries of
    the same logical prepare collapse onto one instruction, while a
    fresh prepare for the same application uses a new salt.
    """
    payload = f"{application_id}|{method}|{salt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class _PrepareRequest:
    application_id: str
    founder_id: str
    amount_usd: int
    currency: str
    target_account_ref: str
    preparation_method: str
    prepared_by: str
    idempotency_key: str


class CapitalDeployment:
    """Service-layer facade over :class:`CapitalBackend` instances.

    Construction is deliberately cheap: the backend is injected per
    call rather than held on the instance so the same service object
    can route to Stripe or to a bank-transfer backend depending on
    the requested method.
    """

    def __init__(self, db: Session, *, publisher: Optional[EventPublisher] = None):
        self._db = db
        self._publisher = publisher or EventPublisher(db)

    # ------------------------------------------------------------------
    # prepare
    # ------------------------------------------------------------------

    def prepare(
        self,
        *,
        backend: CapitalBackend,
        application_id: str,
        founder_id: str,
        amount_usd: int,
        target_account_ref: str,
        preparation_method: str,
        prepared_by: str,
        currency: str = "USD",
        idempotency_key: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> models.InvestmentInstruction:
        """Register a transfer intent. Inert until approved + executed."""
        method = (preparation_method or "").strip().lower()
        if method not in ALLOWED_METHODS:
            raise CapitalDeploymentError(
                f"invalid_preparation_method:{preparation_method!r}"
            )
        if amount_usd <= 0:
            raise CapitalDeploymentError("amount_usd must be positive")
        if not target_account_ref:
            raise CapitalDeploymentError("target_account_ref required")

        key = idempotency_key or compute_idempotency_key(
            application_id, method, salt=uuid.uuid4().hex
        )

        existing = (
            self._db.query(models.InvestmentInstruction)
            .filter(models.InvestmentInstruction.idempotency_key == key)
            .one_or_none()
        )
        if existing is not None:
            return existing

        try:
            response = backend.prepare(
                instruction=_PrepareRequest(
                    application_id=application_id,
                    founder_id=founder_id,
                    amount_usd=amount_usd,
                    currency=currency,
                    target_account_ref=target_account_ref,
                    preparation_method=method,
                    prepared_by=prepared_by,
                    idempotency_key=key,
                )
            )
        except CapitalBackendError as exc:
            raise CapitalDeploymentError(f"backend_prepare_failed:{exc}") from exc

        instruction = models.InvestmentInstruction(
            id=f"ins_{uuid.uuid4().hex[:24]}",
            application_id=application_id,
            founder_id=founder_id,
            amount_usd=int(amount_usd),
            currency=currency,
            target_account_ref=target_account_ref,
            preparation_method=method,
            status="prepared",
            provider_intent_ref=response.provider_intent_ref,
            idempotency_key=key,
            prepared_by=prepared_by,
            prepared_at=_utc_now(),
        )
        self._db.add(instruction)
        self._db.flush()

        self._emit_prepared_event(instruction, trace_id=trace_id)
        return instruction

    # ------------------------------------------------------------------
    # approve
    # ------------------------------------------------------------------

    def approve(
        self,
        *,
        instruction: models.InvestmentInstruction,
        treasurer_id: str,
        note: str = "",
    ) -> models.TreasurerApproval:
        """Record a treasurer approval. Does NOT call any backend."""
        if not treasurer_id:
            raise CapitalDeploymentError("treasurer_id required")
        if instruction.status not in {"prepared", "approved"}:
            raise InstructionStateError(
                f"cannot_approve_in_status:{instruction.status}"
            )

        existing = (
            self._db.query(models.TreasurerApproval)
            .filter(
                models.TreasurerApproval.instruction_id == instruction.id,
                models.TreasurerApproval.treasurer_id == treasurer_id,
            )
            .one_or_none()
        )
        if existing is not None:
            return existing

        approval = models.TreasurerApproval(
            id=f"appr_{uuid.uuid4().hex[:24]}",
            instruction_id=instruction.id,
            treasurer_id=treasurer_id,
            decision="approve",
            note=note,
            created_at=_utc_now(),
        )
        self._db.add(approval)

        # Status transitions to ``approved`` on the first approval. The
        # dual-approval gate is enforced at execute time (so a single
        # treasurer can record their sign-off and a second one can
        # later complete the requirement without a separate "ready"
        # state).
        if instruction.status == "prepared":
            instruction.status = "approved"
            instruction.approved_at = _utc_now()
            instruction.treasurer_id = treasurer_id
        self._db.flush()
        return approval

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    def execute(
        self,
        *,
        backend: CapitalBackend,
        instruction: models.InvestmentInstruction,
        treasurer_id: str,
        trace_id: Optional[str] = None,
    ) -> models.InvestmentInstruction:
        """Dispatch the transfer. Strictly gated on prior approval."""
        if instruction.status != "approved":
            raise InstructionStateError(
                f"cannot_execute_in_status:{instruction.status}"
            )

        approval_count = (
            self._db.query(models.TreasurerApproval)
            .filter(
                models.TreasurerApproval.instruction_id == instruction.id,
                models.TreasurerApproval.decision == "approve",
            )
            .count()
        )
        if approval_count < 1:
            raise InstructionStateError("execute_requires_approval")
        if (
            instruction.amount_usd >= DUAL_APPROVAL_THRESHOLD_USD
            and approval_count < 2
        ):
            raise InstructionStateError(
                "execute_requires_dual_approval"
            )

        try:
            response = backend.execute(instruction=instruction)
        except CapitalBackendError as exc:
            instruction.status = "failed"
            instruction.error_code = str(exc)[:64]
            self._db.flush()
            raise CapitalDeploymentError(f"backend_execute_failed:{exc}") from exc

        instruction.status = "sent"
        instruction.sent_at = _utc_now()
        instruction.treasurer_id = treasurer_id
        # Backends return a confirmation ref distinct from the intent
        # ref; persist alongside intent in the same column so callers
        # can correlate webhook deliveries.
        if response.confirmation_ref:
            instruction.provider_intent_ref = response.confirmation_ref
        self._db.flush()

        self._emit_funded_event(instruction, trace_id=trace_id)
        return instruction

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    def cancel(
        self,
        *,
        instruction: models.InvestmentInstruction,
        reason: str,
    ) -> models.InvestmentInstruction:
        if instruction.status in {"sent"}:
            raise InstructionStateError(
                "cannot_cancel_after_send"
            )
        instruction.status = "cancelled"
        instruction.error_code = (reason or "")[:64]
        self._db.flush()
        return instruction

    # ------------------------------------------------------------------
    # event helpers
    # ------------------------------------------------------------------

    def _emit_prepared_event(
        self,
        instruction: models.InvestmentInstruction,
        *,
        trace_id: Optional[str],
    ) -> None:
        payload = {
            "instruction_id": instruction.id,
            "application_id": instruction.application_id,
            "founder_id": instruction.founder_id,
            "amount_usd": int(instruction.amount_usd),
            "currency": instruction.currency,
            "preparation_method": instruction.preparation_method,
            "target_account_ref": instruction.target_account_ref,
            "provider_intent_ref": instruction.provider_intent_ref,
            "prepared_by": instruction.prepared_by,
        }
        self._publisher._validate_external_schema(
            "investment_funding_prepared", payload
        )
        self._publisher.publish(
            event_type="investment_funding_prepared",
            producer="capital_deployment",
            trace_id=trace_id or f"trace_{uuid.uuid4().hex[:12]}",
            idempotency_key=f"prepare:{instruction.id}",
            payload=payload,
        )

    def _emit_funded_event(
        self,
        instruction: models.InvestmentInstruction,
        *,
        trace_id: Optional[str],
    ) -> None:
        payload = {
            "instruction_id": instruction.id,
            "application_id": instruction.application_id,
            "founder_id": instruction.founder_id,
            "amount_usd": int(instruction.amount_usd),
            "currency": instruction.currency,
            "preparation_method": instruction.preparation_method,
            "provider_intent_ref": instruction.provider_intent_ref,
            "treasurer_id": instruction.treasurer_id,
        }
        self._publisher._validate_external_schema(
            "investment_funded", payload
        )
        self._publisher.publish(
            event_type="investment_funded",
            producer="capital_deployment",
            trace_id=trace_id or f"trace_{uuid.uuid4().hex[:12]}",
            idempotency_key=f"funded:{instruction.id}",
            payload=payload,
        )
