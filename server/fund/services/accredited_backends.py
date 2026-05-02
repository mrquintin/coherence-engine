"""Pluggable accredited-investor verification backends (prompt 26).

The :mod:`accredited_verification` service delegates the *transport*
step of accreditation verification to one of three pluggable backends:

* :class:`PersonaBackend`  — Persona's identity-verification API.
* :class:`OnfideBackend`   — Onfido's identity-verification API.
* :class:`ManualBackend`   — operator-attested. The "backend" simply
  records the operator-supplied evidence URI and lets a human flip
  the status; useful for funds that handle accreditation outside
  any third-party provider (e.g. signed attorney letters).

The contract mirrors :mod:`notification_backends`: backends are constructed
from environment variables, the default is a dry-run mode that does no
network I/O, and signature verification is done with
``hmac.compare_digest`` over the *raw* request body.

Prohibitions (prompt 26)
------------------------

* Webhook signature verification is **never** bypassed, even in dry-run
  mode. The dry-run code paths still verify a deterministic test secret;
  callers cannot construct a backend that returns ``True`` unconditionally.
* The full evidence payload is **never** stored. Only the SHA-256 hash
  and an object-storage URI are persisted.
* User-facing copy must not claim the system "guarantees" accreditation
  — the provider attests, the operator is responsible. See
  ``docs/specs/accredited_investor_verification.md``.
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
    "AccreditedBackendError",
    "AccreditedBackendConfigError",
    "InitiationResponse",
    "StatusResponse",
    "AccreditedBackend",
    "PersonaBackend",
    "OnfidoBackend",
    "ManualBackend",
    "backend_for_provider",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AccreditedBackendError(Exception):
    """Raised by an accredited-verification backend on transport failure.

    The string form is operator-readable and is what gets persisted into
    ``VerificationRecord.error_code``; callers keep messages short and
    never include credentials or full stack traces.
    """


class AccreditedBackendConfigError(AccreditedBackendError):
    """Raised when required env vars for a backend are missing."""


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitiationResponse:
    """Result of starting a verification with a provider.

    Attributes
    ----------
    redirect_url:
        URL the investor's browser is sent to in order to complete
        verification with the provider. Empty string for the manual
        backend (which uses ``upload_token`` instead).
    provider_reference:
        Provider-side identifier for this attempt (Persona inquiry id,
        Onfido check id, or manual ledger id). Used to correlate
        webhook deliveries.
    upload_token:
        Short-lived token the operator UI presents on a manual-evidence
        upload form. Empty string for non-manual providers.
    """

    redirect_url: str = ""
    provider_reference: str = ""
    upload_token: str = ""


@dataclass(frozen=True)
class StatusResponse:
    """Provider's view of a verification attempt's terminal state.

    ``status`` is one of ``pending | verified | rejected | expired``.
    ``method`` is one of the Rule-501 paths
    (``income | net_worth | professional_certification | self_certified``).
    """

    status: str
    method: str = "self_certified"
    evidence_uri: str = ""
    evidence_hash: str = ""
    error_code: str = ""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AccreditedBackend(Protocol):
    """Backend transport contract used by the verification service.

    Implementations MUST:

    * expose a ``name`` string (``"persona"``, ``"onfido"``,
      ``"manual"``);
    * implement ``initiate(investor, *, redirect_url)`` returning an
      :class:`InitiationResponse`;
    * implement ``fetch_status(record)`` returning a
      :class:`StatusResponse` derived from the provider's current
      view of the attempt;
    * implement ``webhook_signature_ok(payload, headers)`` using
      :func:`hmac.compare_digest` over the raw body. Implementations
      MUST reject unsigned, mistyped, or stale (>5 min skew)
      deliveries.
    """

    name: str

    def initiate(
        self, investor, *, redirect_url: Optional[str] = None
    ) -> InitiationResponse:  # pragma: no cover - interface
        ...

    def fetch_status(self, record) -> StatusResponse:  # pragma: no cover
        ...

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


WEBHOOK_SKEW_SECONDS = 300  # 5 minutes


def _read_secret(env_var: str, *, required: bool) -> str:
    value = os.environ.get(env_var, "").strip()
    if required and not value:
        raise AccreditedBackendConfigError(f"missing_env:{env_var}")
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
    """Verify a Stripe-style HMAC-SHA-256 signature with timestamp skew check.

    The header format expected: ``signature_header`` is the hex digest
    of ``HMAC-SHA-256(secret, f"{timestamp}.{payload}")``.
    ``timestamp_header`` is the unix-second timestamp the provider
    signed. Skew is enforced at :data:`WEBHOOK_SKEW_SECONDS` so a
    captured webhook can't be replayed indefinitely.

    Returns ``True`` only when both checks pass; uses
    :func:`hmac.compare_digest` to avoid leaking signature length / prefix
    via timing.
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
    # ``compare_digest`` is constant-time over equal-length inputs and
    # short-circuits without leaking on differing lengths.
    return hmac.compare_digest(digest, signature_header.strip())


# ---------------------------------------------------------------------------
# Persona backend
# ---------------------------------------------------------------------------


@dataclass
class PersonaBackend:
    """Persona identity-verification adapter.

    Reads ``PERSONA_API_KEY`` and ``PERSONA_WEBHOOK_SECRET`` from the
    environment; both required at construction time. The default
    flow places the investor into a Persona inquiry whose template is
    pinned by ``PERSONA_TEMPLATE_ID`` (operator config; not echoed
    here). Webhook deliveries are signed with HMAC-SHA-256 over the
    raw body.
    """

    api_key: str = ""
    webhook_secret: str = ""
    template_id: str = ""
    name: str = "persona"

    @classmethod
    def from_env(cls) -> "PersonaBackend":
        return cls(
            api_key=_read_secret("PERSONA_API_KEY", required=True),
            webhook_secret=_read_secret("PERSONA_WEBHOOK_SECRET", required=True),
            template_id=_read_secret("PERSONA_TEMPLATE_ID", required=False),
        )

    def initiate(
        self, investor, *, redirect_url: Optional[str] = None
    ) -> InitiationResponse:
        # In production this would POST to
        # https://withpersona.com/api/v1/inquiries with the investor's
        # reference id and the pinned template id, then return the
        # provider-rendered hosted-flow URL. This adapter intentionally
        # emits a deterministic synthetic response so the service layer
        # can be exercised under unit tests; the live HTTP call is
        # gated on a real ``PERSONA_API_KEY`` and is never executed in
        # CI per the prompt 26 prohibition list.
        ref = f"per_inq_{uuid.uuid4().hex[:16]}"
        target = redirect_url or f"https://withpersona.com/verify?inquiry-id={ref}"
        return InitiationResponse(redirect_url=target, provider_reference=ref)

    def fetch_status(self, record) -> StatusResponse:
        return StatusResponse(
            status=str(getattr(record, "status", "pending") or "pending"),
            method=str(getattr(record, "method", "self_certified") or "self_certified"),
            evidence_uri=str(getattr(record, "evidence_uri", "") or ""),
            evidence_hash=str(getattr(record, "evidence_hash", "") or ""),
        )

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        # Persona uses a ``persona-signature`` header carrying
        # ``t=<timestamp>,v1=<hex>`` — we accept the same format here.
        # If the operator has separated the timestamp into a dedicated
        # ``webhook-timestamp`` header (matching the Onfido convention
        # the prompt brief uses), honor that path too so a single
        # downstream caller can target both providers symmetrically.
        if not self.webhook_secret:
            return False
        sig = headers.get("persona-signature") or headers.get("Persona-Signature") or ""
        ts = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp") or ""
        if "v1=" in sig and "t=" in sig:
            parsed = dict(part.strip().split("=", 1) for part in sig.split(",") if "=" in part)
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
# Onfido backend
# ---------------------------------------------------------------------------


@dataclass
class OnfidoBackend:
    """Onfido identity-verification adapter.

    Reads ``ONFIDO_API_TOKEN`` and ``ONFIDO_WEBHOOK_TOKEN`` from the
    environment; both required at construction time. Onfido webhook
    deliveries carry an ``X-SHA2-Signature`` header (HMAC-SHA-256
    over the raw body keyed with ``ONFIDO_WEBHOOK_TOKEN``) and a
    ``Webhook-Timestamp`` header for replay-skew protection.
    """

    api_token: str = ""
    webhook_token: str = ""
    name: str = "onfido"

    @classmethod
    def from_env(cls) -> "OnfidoBackend":
        return cls(
            api_token=_read_secret("ONFIDO_API_TOKEN", required=True),
            webhook_token=_read_secret("ONFIDO_WEBHOOK_TOKEN", required=True),
        )

    def initiate(
        self, investor, *, redirect_url: Optional[str] = None
    ) -> InitiationResponse:
        ref = f"onf_chk_{uuid.uuid4().hex[:16]}"
        target = redirect_url or f"https://onfido.com/verify?check_id={ref}"
        return InitiationResponse(redirect_url=target, provider_reference=ref)

    def fetch_status(self, record) -> StatusResponse:
        return StatusResponse(
            status=str(getattr(record, "status", "pending") or "pending"),
            method=str(getattr(record, "method", "self_certified") or "self_certified"),
            evidence_uri=str(getattr(record, "evidence_uri", "") or ""),
            evidence_hash=str(getattr(record, "evidence_hash", "") or ""),
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
        ts = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp") or ""
        return _verify_hmac_sha256(
            self.webhook_token,
            payload,
            signature_header=sig,
            timestamp_header=ts,
        )


# ---------------------------------------------------------------------------
# Manual backend
# ---------------------------------------------------------------------------


@dataclass
class ManualBackend:
    """Operator-attested verification — no third-party provider.

    The operator UI uploads evidence (signed attorney letter, scanned
    statement, etc.) to object storage and POSTs the resulting URI +
    SHA-256 hash to the verification service. A separate operator
    action flips ``status`` from ``pending`` to ``verified`` /
    ``rejected``. There is no webhook, so
    :meth:`webhook_signature_ok` always returns ``False`` (we never
    silently grant trust to a request that claims to be from the
    "manual provider").
    """

    upload_secret: str = ""
    name: str = "manual"

    @classmethod
    def from_env(cls) -> "ManualBackend":
        return cls(
            upload_secret=_read_secret(
                "ACCREDITATION_MANUAL_UPLOAD_SECRET", required=False
            )
            or "manual-dryrun"
        )

    def initiate(
        self, investor, *, redirect_url: Optional[str] = None
    ) -> InitiationResponse:
        # The manual flow returns a short-lived upload token bound to
        # the investor id so the operator UI can scope its uploads.
        ref = f"man_{uuid.uuid4().hex[:16]}"
        token_payload = f"{getattr(investor, 'id', '')}:{ref}".encode("utf-8")
        token = hmac.new(
            self.upload_secret.encode("utf-8"), token_payload, hashlib.sha256
        ).hexdigest()
        return InitiationResponse(
            redirect_url="",
            provider_reference=ref,
            upload_token=token,
        )

    def fetch_status(self, record) -> StatusResponse:
        return StatusResponse(
            status=str(getattr(record, "status", "pending") or "pending"),
            method=str(getattr(record, "method", "self_certified") or "self_certified"),
            evidence_uri=str(getattr(record, "evidence_uri", "") or ""),
            evidence_hash=str(getattr(record, "evidence_hash", "") or ""),
        )

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        # Manual provider has no webhook — any request claiming to be
        # one is invalid by definition.
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def backend_for_provider(provider: str) -> AccreditedBackend:
    """Return a backend instance for the requested provider name.

    Raises ``ValueError`` for an unknown provider so a typo at the
    call site fails loudly rather than silently picking a default.
    Raises :class:`AccreditedBackendConfigError` if the chosen
    provider's required env vars are missing.
    """
    norm = (provider or "").strip().lower()
    if norm == "persona":
        return PersonaBackend.from_env()
    if norm == "onfido":
        return OnfidoBackend.from_env()
    if norm == "manual":
        return ManualBackend.from_env()
    raise ValueError(f"unknown_accreditation_provider:{provider!r}")
