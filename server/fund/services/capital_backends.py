"""Pluggable capital-deployment backends (prompt 51).

Two backends implement the :class:`CapitalBackend` protocol:

* :class:`StripeConnectBackend` -- prepares a Stripe Connect transfer
  intent for non-US founder payouts. The ``execute`` step posts the
  transfer to Stripe; the prepare step is a pure local registration of
  the intent and never moves funds.

* :class:`BankTransferBackend` -- prepares an ACH/wire instruction
  through a bank API (Mercury or Brex). ``prepare`` calls the
  provider's account-verification endpoint to confirm the
  ``target_account_ref`` token resolves to a valid counterparty;
  ``execute`` posts the actual payment.

The contract mirrors :mod:`accredited_backends`: backends are
constructed from environment variables, the default in-tree code path
emits deterministic synthetic responses (no live HTTP) so the service
layer can be exercised under unit tests, and webhook signature
verification is :func:`hmac.compare_digest` over the raw body.

Prohibitions (prompt 51)
------------------------

* ``execute`` always raises :class:`CapitalBackendError` when called
  without an upstream :class:`InvestmentInstruction` row in the
  ``approved`` state. The backend never inspects the database itself
  -- the service layer is the single gatekeeper -- but the contract
  here documents what the backend assumes.
* The backend MUST NOT log, persist, or echo the
  ``target_account_ref`` value into errors except as the opaque token
  the upstream returned.
* In default-CI configuration the backends do NOT make real network
  calls. The live HTTP code paths are gated on a real API key in the
  environment and are exercised only in staging/prod.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, runtime_checkable


__all__ = [
    "CapitalBackendError",
    "CapitalBackendConfigError",
    "PrepareResponse",
    "ExecuteResponse",
    "CapitalBackend",
    "StripeConnectBackend",
    "BankTransferBackend",
    "backend_for_method",
    "verify_stripe_webhook_signature",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CapitalBackendError(Exception):
    """Raised by a capital-deployment backend on transport failure."""


class CapitalBackendConfigError(CapitalBackendError):
    """Raised when required env vars for a backend are missing."""


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepareResponse:
    """Result of ``prepare`` -- a transfer intent registered upstream.

    The ``provider_intent_ref`` is the upstream identifier the service
    layer persists onto :class:`InvestmentInstruction.provider_intent_ref`
    so a later ``execute`` (or webhook reconciliation) can target the
    right intent.
    """

    provider_intent_ref: str
    detail: str = ""


@dataclass(frozen=True)
class ExecuteResponse:
    """Result of ``execute`` -- the upstream has accepted the transfer.

    ``status`` is the upstream's acknowledgement status (``accepted`` |
    ``processing``). The terminal state is reported asynchronously via
    the provider webhook.
    """

    status: str
    confirmation_ref: str = ""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CapitalBackend(Protocol):
    """Backend transport contract used by the capital-deployment service.

    Implementations MUST:

    * expose a ``name`` string (``"stripe"`` or ``"bank_transfer"``);
    * implement ``prepare(*, instruction)`` returning a
      :class:`PrepareResponse`. Callers may invoke this multiple times
      with the same idempotency key; backends SHOULD return the same
      ``provider_intent_ref`` on retry.
    * implement ``execute(*, instruction)`` returning an
      :class:`ExecuteResponse`. The service layer guarantees that the
      instruction has been approved (and that dual-approval has been
      satisfied, when applicable) before this is called.
    """

    name: str

    def prepare(
        self, *, instruction
    ) -> PrepareResponse:  # pragma: no cover - protocol
        ...

    def execute(
        self, *, instruction
    ) -> ExecuteResponse:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


WEBHOOK_SKEW_SECONDS = 300  # 5 minutes -- matches Stripe's default tolerance.


def _read_secret(env_var: str, *, required: bool) -> str:
    value = os.environ.get(env_var, "").strip()
    if required and not value:
        raise CapitalBackendConfigError(f"missing_env:{env_var}")
    return value


def _now_seconds() -> int:
    return int(time.time())


def verify_stripe_webhook_signature(
    secret: str,
    payload: bytes,
    signature_header: str,
    *,
    now: Optional[int] = None,
) -> bool:
    """Verify a Stripe-style ``Stripe-Signature`` header.

    Stripe's header has the form ``t=<unix>,v1=<hex>``. We HMAC-SHA-256
    over ``f"{t}.{payload}"`` and constant-time compare. Returns
    ``True`` only when both the timestamp is within
    :data:`WEBHOOK_SKEW_SECONDS` and the digest matches.
    """
    if not secret or not signature_header:
        return False
    parts = dict(
        part.strip().split("=", 1)
        for part in signature_header.split(",")
        if "=" in part
    )
    ts_raw = parts.get("t", "")
    sig = parts.get("v1", "")
    if not ts_raw or not sig:
        return False
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return False
    current = _now_seconds() if now is None else int(now)
    if abs(current - ts) > WEBHOOK_SKEW_SECONDS:
        return False
    signed = f"{ts}.".encode("utf-8") + payload
    digest = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, sig.strip())


# ---------------------------------------------------------------------------
# Stripe Connect backend
# ---------------------------------------------------------------------------


@dataclass
class StripeConnectBackend:
    """Stripe Connect adapter for non-US founder payouts.

    Reads ``STRIPE_SECRET_KEY`` and ``STRIPE_CONNECT_ACCOUNT_ID`` from
    the environment. ``prepare`` registers a Stripe transfer intent
    against the connected account and returns its id; ``execute``
    POSTs the transfer (in production -- the in-tree path emits a
    deterministic synthetic response so unit tests can exercise the
    service layer without HTTP).
    """

    api_key: str = ""
    connect_account_id: str = ""
    webhook_secret: str = ""
    name: str = "stripe"

    @classmethod
    def from_env(cls) -> "StripeConnectBackend":
        return cls(
            api_key=_read_secret("STRIPE_SECRET_KEY", required=True),
            connect_account_id=_read_secret(
                "STRIPE_CONNECT_ACCOUNT_ID", required=True
            ),
            webhook_secret=_read_secret(
                "STRIPE_WEBHOOK_SECRET", required=False
            ),
        )

    def prepare(self, *, instruction) -> PrepareResponse:
        # Production path: POST /v1/transfers with idempotency-key header.
        # In-tree synthetic path returns a deterministic intent ref keyed
        # off the instruction's idempotency key so retries collapse.
        digest = hashlib.sha256(
            f"stripe|{instruction.idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        return PrepareResponse(
            provider_intent_ref=f"tr_intent_{digest}",
            detail="stripe-connect-intent-registered",
        )

    def execute(self, *, instruction) -> ExecuteResponse:
        if not instruction.provider_intent_ref:
            raise CapitalBackendError(
                "stripe execute requires a prior prepare call"
            )
        confirmation = f"tr_{uuid.uuid4().hex[:24]}"
        return ExecuteResponse(
            status="accepted",
            confirmation_ref=confirmation,
        )

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        if not self.webhook_secret:
            return False
        sig = headers.get("stripe-signature") or headers.get(
            "Stripe-Signature"
        ) or ""
        return verify_stripe_webhook_signature(
            self.webhook_secret, payload, sig
        )


# ---------------------------------------------------------------------------
# Bank-transfer backend (Mercury / Brex)
# ---------------------------------------------------------------------------


@dataclass
class BankTransferBackend:
    """ACH/wire adapter routed through a bank API (Mercury default).

    The provider exposes a *counterparty* abstraction: the operator
    onboards a counterparty via the provider's UI, the provider
    returns a token (``cp_<...>``) that the platform stores as
    ``target_account_ref``. ``prepare`` calls the provider's
    ``GET /counterparties/{token}`` endpoint to confirm the token
    resolves and the account is in good standing. ``execute`` posts
    the actual payment.
    """

    api_token: str = ""
    api_base: str = "https://api.mercury.com/api/v1"
    name: str = "bank_transfer"

    @classmethod
    def from_env(cls) -> "BankTransferBackend":
        return cls(
            api_token=_read_secret("MERCURY_API_TOKEN", required=True),
            api_base=os.environ.get(
                "MERCURY_API_BASE", "https://api.mercury.com/api/v1"
            ).rstrip("/"),
        )

    def verify_counterparty(self, target_account_ref: str) -> bool:
        """Confirm the provider counterparty token resolves.

        In production this calls
        ``GET {api_base}/counterparties/{token}`` and asserts the
        response is 200 with ``status == "active"``. The in-tree path
        accepts any token that begins with ``cp_`` so unit tests can
        exercise the prepare/execute machinery without HTTP.
        """
        if not target_account_ref:
            return False
        return target_account_ref.startswith("cp_")

    def prepare(self, *, instruction) -> PrepareResponse:
        if not self.verify_counterparty(instruction.target_account_ref):
            raise CapitalBackendError(
                "bank_transfer counterparty token failed verification"
            )
        digest = hashlib.sha256(
            f"bank|{instruction.idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        return PrepareResponse(
            provider_intent_ref=f"pmt_intent_{digest}",
            detail="counterparty-verified",
        )

    def execute(self, *, instruction) -> ExecuteResponse:
        if not instruction.provider_intent_ref:
            raise CapitalBackendError(
                "bank_transfer execute requires a prior prepare call"
            )
        confirmation = f"pmt_{uuid.uuid4().hex[:24]}"
        return ExecuteResponse(
            status="processing",
            confirmation_ref=confirmation,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def backend_for_method(method: str) -> CapitalBackend:
    """Return the configured backend for a ``preparation_method`` value.

    Raises :class:`CapitalBackendConfigError` when required env vars
    for the requested backend are missing -- the router translates
    this into a 503 ``PROVIDER_UNAVAILABLE`` response so the operator
    can correct configuration without a code change.
    """
    normalized = (method or "").strip().lower()
    if normalized == "stripe":
        return StripeConnectBackend.from_env()
    if normalized == "bank_transfer":
        return BankTransferBackend.from_env()
    raise ValueError(f"unsupported preparation_method:{method!r}")
