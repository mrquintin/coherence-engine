"""Cap-table integration service (prompt 68).

Owns the local :class:`CapTableIssuance` ledger and the sync to a
configured provider backend (Carta primary, Pulley alternate). The
service is **record-keeping only** -- it does not unilaterally issue
securities. An issuance row may be created only after the upstream
investment workflow has reached the operator-caused terminal state
for both:

* the SAFE / term-sheet :class:`SignatureRequest` is ``signed``
  (prompt 52); and
* the corresponding :class:`InvestmentInstruction` is ``sent``
  (prompt 51).

Lifecycle:

    pending --(provider sync)--> recorded --(reconcile)--> reconciled
                                          \\
                                           +--> failed (terminal)

Idempotency
-----------

The ``idempotency_key`` is derived from
``(application_id, instrument_type, salt)`` where ``salt`` is the
caller-provided context (typically the
:attr:`InvestmentInstruction.id`). Repeat calls with the same key
collapse onto a single row -- the second call returns the existing
row without a second backend dispatch.

Reconciliation (prompt 68 prohibition)
--------------------------------------

:meth:`CapTableService.reconcile` reads back every ``recorded`` row
from the provider and compares the numeric fields. A divergence is
reported in the returned :class:`ReconciliationReport`; the local
row is **never** silently mutated to match the provider. The
operator decides how to resolve the divergence (typically by
correcting the provider record out of band).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services.cap_table_backends import (
    CapTableBackend,
    CapTableBackendError,
    ProviderRecord,
    ProviderResponse,
)


__all__ = [
    "CapTableService",
    "CapTableError",
    "PreconditionsNotMet",
    "ReconciliationReport",
    "ReconciliationFinding",
    "ALLOWED_INSTRUMENT_TYPES",
    "ALLOWED_STATUSES",
    "TERMINAL_STATUSES",
    "compute_idempotency_key",
    "preconditions_satisfied",
]


_LOG = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


ALLOWED_INSTRUMENT_TYPES = frozenset(
    {"safe_post_money", "safe_pre_money", "priced_round_preferred"}
)

ALLOWED_STATUSES = frozenset(
    {"pending", "recorded", "reconciled", "failed"}
)

TERMINAL_STATUSES = frozenset({"reconciled", "failed"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CapTableError(Exception):
    """Raised by :class:`CapTableService` for non-recoverable failures."""


class PreconditionsNotMet(CapTableError):
    """Raised when the upstream signed-SAFE + sent-instruction gates fail.

    This is the load-bearing prompt-68 safety check: a cap-table
    issuance MUST NOT be recorded against an application unless the
    operator has caused it through the normal investment workflow.
    """


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationFinding:
    """A single divergence between local row and provider record."""

    issuance_id: str
    application_id: str
    provider: str
    field: str
    local_value: object
    provider_value: object


@dataclass
class ReconciliationReport:
    """Result of :meth:`CapTableService.reconcile`."""

    checked: int = 0
    reconciled: int = 0
    divergent: List[ReconciliationFinding] = field(default_factory=list)
    missing_remote: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.divergent and not self.missing_remote


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_idempotency_key(
    application_id: str,
    instrument_type: str,
    salt: str,
) -> str:
    """Deterministic idempotency key for ``record_issuance``.

    The salt is typically the
    :attr:`InvestmentInstruction.id` so two distinct funded
    instructions for the same (application, instrument) produce two
    distinct issuance rows, while a retry of the same logical record
    collapses onto one.
    """
    payload = f"{application_id}|{instrument_type}|{salt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def preconditions_satisfied(
    db: Session, *, application_id: str
) -> tuple[Optional[models.SignatureRequest], Optional[models.InvestmentInstruction]]:
    """Return the matching signed SAFE + sent instruction, or ``(None, None)``.

    The cap-table sync may proceed only when *both* are present. The
    return shape is the (signature, instruction) pair so the caller
    can read provenance fields off the rows; ``(None, None)`` means
    the gate is not satisfied and no issuance row should be created.
    """
    signature = (
        db.query(models.SignatureRequest)
        .filter(
            models.SignatureRequest.application_id == application_id,
            models.SignatureRequest.status == "signed",
        )
        .order_by(models.SignatureRequest.completed_at.desc())
        .first()
    )
    instruction = (
        db.query(models.InvestmentInstruction)
        .filter(
            models.InvestmentInstruction.application_id == application_id,
            models.InvestmentInstruction.status == "sent",
        )
        .order_by(models.InvestmentInstruction.sent_at.desc())
        .first()
    )
    if signature is None or instruction is None:
        return None, None
    return signature, instruction


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class CapTableService:
    """Owns the cap-table issuance lifecycle.

    The backend is injected per call rather than held on the
    instance so the same service object can route to Carta or to
    Pulley depending on the operator's configuration.
    """

    db: Session

    # ----- record -------------------------------------------------

    def record_issuance(
        self,
        *,
        backend: CapTableBackend,
        application_id: str,
        instrument_type: str,
        amount_usd: int,
        valuation_cap_usd: int = 0,
        discount: float = 0.0,
        board_consent_uri: str = "",
        idempotency_key: Optional[str] = None,
        verify_preconditions: bool = True,
    ) -> models.CapTableIssuance:
        """Create or fetch a :class:`CapTableIssuance` and sync to provider.

        The default ``verify_preconditions=True`` enforces the load-
        bearing prompt-68 safety check that the application has both
        a signed SAFE and a sent investment instruction before any
        issuance is recorded. Callers that have already verified the
        preconditions (e.g. the ``ApplicationService`` hook that runs
        only after both events have been observed) MAY pass ``False``
        to skip the redundant DB read.
        """
        instrument = (instrument_type or "").strip().lower()
        if instrument not in ALLOWED_INSTRUMENT_TYPES:
            raise CapTableError(
                f"invalid_instrument_type:{instrument_type!r}"
            )
        if amount_usd <= 0:
            raise CapTableError("amount_usd must be positive")

        if verify_preconditions:
            sig, ins = preconditions_satisfied(
                self.db, application_id=application_id
            )
            if sig is None or ins is None:
                raise PreconditionsNotMet(
                    "cap_table_record_requires_signed_safe_and_sent_instruction"
                )

        key = idempotency_key or compute_idempotency_key(
            application_id, instrument, salt=uuid.uuid4().hex
        )

        existing = (
            self.db.query(models.CapTableIssuance)
            .filter(models.CapTableIssuance.idempotency_key == key)
            .one_or_none()
        )
        if existing is not None:
            # Idempotent retry: do NOT call the backend again. The
            # provider's own idempotency may also collapse the call,
            # but skipping the dispatch saves a round-trip and keeps
            # the local audit trail single-sourced from this row.
            return existing

        row = models.CapTableIssuance(
            id=f"cti_{uuid.uuid4().hex[:24]}",
            application_id=application_id,
            instrument_type=instrument,
            amount_usd=int(amount_usd),
            valuation_cap_usd=int(valuation_cap_usd or 0),
            discount=float(discount or 0.0),
            board_consent_uri=board_consent_uri or "",
            provider=backend.name,
            provider_issuance_id="",
            status="pending",
            idempotency_key=key,
            created_at=_utc_now(),
        )
        self.db.add(row)
        self.db.flush()

        try:
            response: ProviderResponse = backend.record_issuance(issuance=row)
        except CapTableBackendError as exc:
            row.status = "failed"
            self.db.flush()
            raise CapTableError(f"backend_record_failed:{exc}") from exc

        row.provider_issuance_id = response.provider_issuance_id
        row.status = "recorded"
        row.recorded_at = _utc_now()
        self.db.flush()
        return row

    # ----- fetch --------------------------------------------------

    def fetch_local(
        self, *, application_id: str
    ) -> Sequence[models.CapTableIssuance]:
        return (
            self.db.query(models.CapTableIssuance)
            .filter(
                models.CapTableIssuance.application_id == application_id
            )
            .order_by(models.CapTableIssuance.created_at.asc())
            .all()
        )

    # ----- reconcile ----------------------------------------------

    def reconcile(
        self,
        *,
        backend: CapTableBackend,
    ) -> ReconciliationReport:
        """Compare every ``recorded`` local row to the provider's record.

        Divergences are reported in the returned report; the local
        row is NEVER silently rewritten to match the provider
        (prompt 68 prohibition). Rows whose numeric fields all
        agree transition ``recorded -> reconciled``.
        """
        report = ReconciliationReport()
        rows = (
            self.db.query(models.CapTableIssuance)
            .filter(
                models.CapTableIssuance.provider == backend.name,
                models.CapTableIssuance.status == "recorded",
            )
            .all()
        )
        for row in rows:
            report.checked += 1
            try:
                remote = backend.fetch_issuance(
                    provider_issuance_id=row.provider_issuance_id
                )
            except CapTableBackendError:
                report.missing_remote.append(row.id)
                continue
            findings = _diff_record(row, remote)
            if findings:
                report.divergent.extend(findings)
                continue
            row.status = "reconciled"
            self.db.flush()
            report.reconciled += 1
        return report


def _diff_record(
    row: models.CapTableIssuance, remote: ProviderRecord
) -> List[ReconciliationFinding]:
    findings: List[ReconciliationFinding] = []
    comparisons = (
        ("instrument_type", row.instrument_type, remote.instrument_type),
        ("amount_usd", int(row.amount_usd), int(remote.amount_usd)),
        (
            "valuation_cap_usd",
            int(row.valuation_cap_usd or 0),
            int(remote.valuation_cap_usd or 0),
        ),
        ("discount", float(row.discount or 0.0), float(remote.discount or 0.0)),
    )
    for field_name, local_value, remote_value in comparisons:
        if local_value != remote_value:
            findings.append(
                ReconciliationFinding(
                    issuance_id=row.id,
                    application_id=row.application_id,
                    provider=row.provider,
                    field=field_name,
                    local_value=local_value,
                    provider_value=remote_value,
                )
            )
    return findings
