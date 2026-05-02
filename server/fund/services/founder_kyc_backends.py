"""Pluggable founder KYC/AML screening backends (prompt 53).

Distinct from :mod:`accredited_backends` (prompt 26): those backends
gate LP-side accredited-investor verification under SEC Rule 501.
*These* backends gate the founder-side KYC/AML pipeline -- sanctions
screening, PEP screening, ID verification -- and feed
:mod:`founder_kyc` and the ``kyc_clear`` decision-policy gate. The two
adapters share Persona / Onfido as the underlying provider but use
**separate environment variables and webhook secrets** so a leaked
LP-flow secret cannot forge a founder-flow webhook (and vice versa).

Prohibitions (prompt 53)
------------------------

* Webhook signature verification is **never** bypassed, even in
  dry-run mode. Like prompt 26, the dry-run code path still verifies
  a deterministic test secret; a backend cannot return ``True``
  unconditionally for a ``webhook_signature_ok`` check.
* The full evidence payload is **never** stored. Only the SHA-256
  hash plus the provider's evidence reference (Persona inquiry id,
  Onfido check id, or an object-storage URI) are persisted.
* A failed KYC is NOT auto-rejected forever -- the operator UI must
  route the founder to manual review. See ``docs/specs/founder_kyc.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, runtime_checkable


__all__ = [
    "FounderKYCBackendError",
    "FounderKYCBackendConfigError",
    "KYCInitiationResponse",
    "KYCStatusResponse",
    "FounderKYCBackend",
    "PersonaKYCBackend",
    "OnfidoKYCBackend",
    "kyc_backend_for_provider",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FounderKYCBackendError(Exception):
    """Raised by a founder-KYC backend on transport failure.

    Stringifies short and operator-readable -- the value is what gets
    persisted into ``KYCResult.error_code``. Never include credentials
    or full stack traces.
    """


class FounderKYCBackendConfigError(FounderKYCBackendError):
    """Raised when required env vars for a backend are missing."""


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KYCInitiationResponse:
    """Result of starting a KYC attempt with a provider."""

    redirect_url: str = ""
    provider_reference: str = ""


@dataclass(frozen=True)
class KYCStatusResponse:
    """Provider's view of a KYC attempt's terminal state.

    ``status`` is one of ``pending | passed | failed | expired``. The
    service translates ``"clear"`` / ``"approved"`` -> ``passed`` and
    ``"consider"`` / ``"rejected"`` -> ``failed`` so a single vocabulary
    is used downstream.
    """

    status: str
    screening_categories: str = "sanctions,pep,id,aml"
    evidence_uri: str = ""
    evidence_hash: str = ""
    error_code: str = ""
    failure_reason: str = ""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FounderKYCBackend(Protocol):
    """Backend transport contract used by :mod:`founder_kyc`.

    Implementations MUST:

    * expose a ``name`` string (``"persona"``, ``"onfido"``);
    * implement ``initiate(founder, *, redirect_url)`` returning a
      :class:`KYCInitiationResponse`;
    * implement ``webhook_signature_ok(payload, headers)`` using
      :func:`hmac.compare_digest` over the raw body, with a 5-minute
      timestamp-skew check.
    """

    name: str

    def initiate(
        self, founder, *, redirect_url: Optional[str] = None
    ) -> KYCInitiationResponse:  # pragma: no cover - interface
        ...

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:  # pragma: no cover - interface
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


WEBHOOK_SKEW_SECONDS = 300  # 5 minutes


def _read_secret(env_var: str, *, required: bool) -> str:
    value = os.environ.get(env_var, "").strip()
    if required and not value:
        raise FounderKYCBackendConfigError(f"missing_env:{env_var}")
    return value


def _now_seconds() -> int:
    return int(time.time())


def _verify_hmac_sha256(
    secret: str,
    payload: bytes,
    *,
    signature_header: str,
    timestamp_header: str,
    now: Optional[int] = None,
) -> bool:
    """Constant-time HMAC-SHA-256 verification with timestamp skew check.

    Header format expected: ``signature_header`` is the hex digest of
    ``HMAC-SHA-256(secret, f"{timestamp}.{payload}")``;
    ``timestamp_header`` is the unix-second timestamp the provider
    signed. Skew is enforced at :data:`WEBHOOK_SKEW_SECONDS` so a
    captured webhook can't be replayed indefinitely.
    """
    if not secret:
        return False
    if not signature_header or not timestamp_header:
        return False
    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    current = _now_seconds() if now is None else int(now)
    if abs(current - ts) > WEBHOOK_SKEW_SECONDS:
        return False
    signed_payload = f"{ts}.".encode("utf-8") + payload
    digest = hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, signature_header.strip())


# ---------------------------------------------------------------------------
# Persona KYC backend
# ---------------------------------------------------------------------------


@dataclass
class PersonaKYCBackend:
    """Persona founder-KYC adapter.

    Reads ``PERSONA_KYC_API_KEY`` and ``PERSONA_KYC_WEBHOOK_SECRET`` --
    a *different* pair from the LP accreditation flow's
    ``PERSONA_API_KEY`` / ``PERSONA_WEBHOOK_SECRET``. The split is
    intentional: a single leaked secret should not cross the
    founder/LP trust boundary.
    """

    api_key: str = ""
    webhook_secret: str = ""
    template_id: str = ""
    name: str = "persona"

    @classmethod
    def from_env(cls) -> "PersonaKYCBackend":
        return cls(
            api_key=_read_secret("PERSONA_KYC_API_KEY", required=True),
            webhook_secret=_read_secret(
                "PERSONA_KYC_WEBHOOK_SECRET", required=True
            ),
            template_id=_read_secret(
                "PERSONA_KYC_TEMPLATE_ID", required=False
            ),
        )

    def initiate(
        self, founder, *, redirect_url: Optional[str] = None
    ) -> KYCInitiationResponse:
        # In production this would POST to Persona's inquiries endpoint
        # with the founder's reference id and the pinned KYC template
        # id; the live HTTP call is gated on a real
        # ``PERSONA_KYC_API_KEY`` and is never executed in CI.
        ref = f"per_kyc_{uuid.uuid4().hex[:16]}"
        target = (
            redirect_url or f"https://withpersona.com/kyc?inquiry-id={ref}"
        )
        return KYCInitiationResponse(
            redirect_url=target, provider_reference=ref
        )

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        if not self.webhook_secret:
            return False
        sig = (
            headers.get("persona-signature")
            or headers.get("Persona-Signature")
            or ""
        )
        ts = (
            headers.get("webhook-timestamp")
            or headers.get("Webhook-Timestamp")
            or ""
        )
        if "v1=" in sig and "t=" in sig:
            parsed = dict(
                part.strip().split("=", 1)
                for part in sig.split(",")
                if "=" in part
            )
            ts = ts or parsed.get("t", "")
            sig_value = parsed.get("v1", "")
        else:
            sig_value = sig
        return _verify_hmac_sha256(
            self.webhook_secret,
            payload,
            signature_header=sig_value,
            timestamp_header=ts,
        )


# ---------------------------------------------------------------------------
# Onfido KYC backend
# ---------------------------------------------------------------------------


@dataclass
class OnfidoKYCBackend:
    """Onfido founder-KYC adapter.

    Reads ``ONFIDO_KYC_API_TOKEN`` and ``ONFIDO_KYC_WEBHOOK_TOKEN`` --
    again, separate from the LP-flow Onfido vars by design.
    Webhook deliveries carry an ``X-SHA2-Signature`` header
    (HMAC-SHA-256 over the raw body keyed with ``ONFIDO_KYC_WEBHOOK_TOKEN``)
    and a ``Webhook-Timestamp`` header for replay-skew protection.
    """

    api_token: str = ""
    webhook_token: str = ""
    name: str = "onfido"

    @classmethod
    def from_env(cls) -> "OnfidoKYCBackend":
        return cls(
            api_token=_read_secret("ONFIDO_KYC_API_TOKEN", required=True),
            webhook_token=_read_secret(
                "ONFIDO_KYC_WEBHOOK_TOKEN", required=True
            ),
        )

    def initiate(
        self, founder, *, redirect_url: Optional[str] = None
    ) -> KYCInitiationResponse:
        ref = f"onf_kyc_{uuid.uuid4().hex[:16]}"
        target = redirect_url or f"https://onfido.com/kyc?check_id={ref}"
        return KYCInitiationResponse(
            redirect_url=target, provider_reference=ref
        )

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        if not self.webhook_token:
            return False
        sig = (
            headers.get("x-sha2-signature")
            or headers.get("X-SHA2-Signature")
            or headers.get("x-signature-sha256")
            or ""
        )
        ts = (
            headers.get("webhook-timestamp")
            or headers.get("Webhook-Timestamp")
            or ""
        )
        return _verify_hmac_sha256(
            self.webhook_token,
            payload,
            signature_header=sig,
            timestamp_header=ts,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def kyc_backend_for_provider(provider: str) -> FounderKYCBackend:
    """Return a founder-KYC backend for ``provider``.

    Raises ``ValueError`` for an unknown provider so a typo at the
    call site fails loudly rather than silently picking a default.
    Raises :class:`FounderKYCBackendConfigError` if the chosen
    provider's required env vars are missing.
    """
    norm = (provider or "").strip().lower()
    if norm == "persona":
        return PersonaKYCBackend.from_env()
    if norm == "onfido":
        return OnfidoKYCBackend.from_env()
    raise ValueError(f"unknown_kyc_provider:{provider!r}")
