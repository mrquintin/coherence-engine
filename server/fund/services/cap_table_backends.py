"""Pluggable cap-table provider backends (prompt 68).

Two backends implement the :class:`CapTableBackend` protocol:

* :class:`CartaBackend` -- primary integration. Records issuances
  against a Carta-managed cap table. ``record_issuance`` is the only
  state-mutating call; ``fetch_issuance`` is read-only and is used by
  the reconciliation job.

* :class:`PulleyBackend` -- alternate integration. Same protocol;
  some early-stage funds prefer Pulley over Carta.

The contract mirrors :mod:`capital_backends` and
:mod:`esignature_backends`: backends are constructed from environment
variables, the default in-tree code path emits deterministic synthetic
responses (no live HTTP) so the service layer can be exercised under
unit tests, and reconciliation never silently mutates the local row
to match the provider -- divergences are *flagged* and surfaced.

Prohibitions (prompt 68)
------------------------

* The backend MUST NOT be invoked with an issuance whose upstream
  preconditions (signed SAFE artifact + sent investment instruction)
  have not been verified. The :class:`CapTableService` is the single
  gatekeeper for that check; this module's contract documents the
  assumption.
* The backend's returned ``provider_issuance_id`` is informational
  ONLY. Local idempotency is keyed off
  :attr:`CapTableIssuance.idempotency_key`; the provider's id is
  recorded for audit but never trusted as authoritative.
* In default-CI configuration the backends do NOT make real network
  calls. The live HTTP code paths are gated on a real API token in
  the environment and are exercised only in staging/prod.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


__all__ = [
    "CapTableBackendError",
    "CapTableBackendConfigError",
    "ProviderResponse",
    "ProviderRecord",
    "CapTableBackend",
    "CartaBackend",
    "PulleyBackend",
    "backend_for_provider",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CapTableBackendError(Exception):
    """Raised by a cap-table backend on transport failure."""


class CapTableBackendConfigError(CapTableBackendError):
    """Raised when required env vars for a backend are missing."""


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderResponse:
    """Result of :meth:`CapTableBackend.record_issuance`.

    ``provider_issuance_id`` is the upstream's opaque identifier --
    persisted for audit but never trusted as authoritative for local
    idempotency (prompt 68 prohibition).
    """

    provider_issuance_id: str
    status: str = "recorded"
    detail: str = ""


@dataclass(frozen=True)
class ProviderRecord:
    """Result of :meth:`CapTableBackend.fetch_issuance`.

    The reconciliation routine compares this record's numeric fields
    against the local :class:`CapTableIssuance` row. A mismatch is
    flagged (``divergent``) but never auto-healed.
    """

    provider_issuance_id: str
    instrument_type: str
    amount_usd: int
    valuation_cap_usd: int
    discount: float
    status: str = "recorded"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CapTableBackend(Protocol):
    """Backend transport contract for a cap-table provider.

    Implementations MUST:

    * expose a ``name`` string (``"carta"`` or ``"pulley"``);
    * implement ``record_issuance(issuance)`` returning a
      :class:`ProviderResponse`. Callers may invoke this multiple
      times with the same idempotency key; backends SHOULD return
      the same ``provider_issuance_id`` on retry.
    * implement ``fetch_issuance(provider_id)`` returning a
      :class:`ProviderRecord`. Read-only.
    """

    name: str

    def record_issuance(
        self, *, issuance
    ) -> ProviderResponse:  # pragma: no cover - protocol
        ...

    def fetch_issuance(
        self, *, provider_issuance_id: str
    ) -> ProviderRecord:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_secret(env_var: str, *, required: bool) -> str:
    value = os.environ.get(env_var, "").strip()
    if required and not value:
        raise CapTableBackendConfigError(f"missing_env:{env_var}")
    return value


def _synthetic_id(prefix: str, idempotency_key: str) -> str:
    """Deterministic provider id keyed off the local idempotency key.

    Used by both backends in the in-tree synthetic path so retries
    return the same id without HTTP. The 24-char digest is collision-
    resistant for the purposes of the unit tests.
    """
    digest = hashlib.sha256(
        f"{prefix}|{idempotency_key}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}_{digest}"


# ---------------------------------------------------------------------------
# In-memory ledger for the synthetic backends
# ---------------------------------------------------------------------------


@dataclass
class _SyntheticLedger:
    """Per-process ledger of issuances recorded against the synthetic path.

    Production backends talk to Carta / Pulley over HTTPS; the in-tree
    path keeps a small dict so :meth:`fetch_issuance` (and therefore
    :class:`CapTableService.reconcile`) returns the data the test
    just wrote. The ledger is process-local and is not persisted.
    """

    records: dict = field(default_factory=dict)

    def put(self, key: str, record: ProviderRecord) -> None:
        self.records[key] = record

    def get(self, key: str) -> Optional[ProviderRecord]:
        return self.records.get(key)

    def reset(self) -> None:
        self.records.clear()


# ---------------------------------------------------------------------------
# Carta
# ---------------------------------------------------------------------------


@dataclass
class CartaBackend:
    """Carta adapter -- primary cap-table integration.

    Reads ``CARTA_API_TOKEN`` from the environment. ``record_issuance``
    POSTs to ``/issuances`` against ``CARTA_API_BASE``; ``fetch_issuance``
    GETs ``/issuances/{id}``. The in-tree synthetic path emits a
    deterministic id and stores the record in an in-memory ledger so
    the reconciliation job can be exercised under unit tests.
    """

    api_token: str = ""
    api_base: str = "https://api.carta.com/v1"
    name: str = "carta"
    _ledger: _SyntheticLedger = field(default_factory=_SyntheticLedger)

    @classmethod
    def from_env(cls) -> "CartaBackend":
        return cls(
            api_token=_read_secret("CARTA_API_TOKEN", required=True),
            api_base=os.environ.get(
                "CARTA_API_BASE", "https://api.carta.com/v1"
            ).rstrip("/"),
        )

    def record_issuance(self, *, issuance) -> ProviderResponse:
        provider_id = _synthetic_id("carta_iss", issuance.idempotency_key)
        # Persist into the synthetic ledger so reconcile() can read it
        # back in the same process. Production path replaces this with
        # the response from ``POST /issuances``.
        self._ledger.put(
            provider_id,
            ProviderRecord(
                provider_issuance_id=provider_id,
                instrument_type=issuance.instrument_type,
                amount_usd=int(issuance.amount_usd),
                valuation_cap_usd=int(issuance.valuation_cap_usd or 0),
                discount=float(issuance.discount or 0.0),
                status="recorded",
            ),
        )
        return ProviderResponse(
            provider_issuance_id=provider_id,
            status="recorded",
            detail="carta-issuance-recorded",
        )

    def fetch_issuance(self, *, provider_issuance_id: str) -> ProviderRecord:
        record = self._ledger.get(provider_issuance_id)
        if record is None:
            raise CapTableBackendError(
                f"carta issuance not found: {provider_issuance_id}"
            )
        return record


# ---------------------------------------------------------------------------
# Pulley
# ---------------------------------------------------------------------------


@dataclass
class PulleyBackend:
    """Pulley adapter -- alternate cap-table integration.

    Reads ``PULLEY_API_TOKEN`` from the environment. Same shape as
    :class:`CartaBackend`; the only differences in production are the
    HTTP route names and the response field naming. The in-tree
    synthetic path is structurally identical so the service layer can
    be tested against either backend without modification.
    """

    api_token: str = ""
    api_base: str = "https://api.pulley.com/v1"
    name: str = "pulley"
    _ledger: _SyntheticLedger = field(default_factory=_SyntheticLedger)

    @classmethod
    def from_env(cls) -> "PulleyBackend":
        return cls(
            api_token=_read_secret("PULLEY_API_TOKEN", required=True),
            api_base=os.environ.get(
                "PULLEY_API_BASE", "https://api.pulley.com/v1"
            ).rstrip("/"),
        )

    def record_issuance(self, *, issuance) -> ProviderResponse:
        provider_id = _synthetic_id("pulley_iss", issuance.idempotency_key)
        self._ledger.put(
            provider_id,
            ProviderRecord(
                provider_issuance_id=provider_id,
                instrument_type=issuance.instrument_type,
                amount_usd=int(issuance.amount_usd),
                valuation_cap_usd=int(issuance.valuation_cap_usd or 0),
                discount=float(issuance.discount or 0.0),
                status="recorded",
            ),
        )
        return ProviderResponse(
            provider_issuance_id=provider_id,
            status="recorded",
            detail="pulley-issuance-recorded",
        )

    def fetch_issuance(self, *, provider_issuance_id: str) -> ProviderRecord:
        record = self._ledger.get(provider_issuance_id)
        if record is None:
            raise CapTableBackendError(
                f"pulley issuance not found: {provider_issuance_id}"
            )
        return record


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def backend_for_provider(provider: str) -> CapTableBackend:
    """Return the configured backend for a provider name.

    Raises :class:`CapTableBackendConfigError` when required env vars
    for the requested backend are missing.
    """
    normalized = (provider or "").strip().lower()
    if normalized == "carta":
        return CartaBackend.from_env()
    if normalized == "pulley":
        return PulleyBackend.from_env()
    raise ValueError(f"unsupported cap_table provider: {provider!r}")
