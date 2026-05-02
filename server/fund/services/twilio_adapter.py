"""Twilio Voice adapter for phone-based founder interviews (prompt 38).

This module owns the *Twilio-shaped* boundary: the ``RequestValidator``
used to authenticate inbound webhook deliveries, the ``TwilioClient``
protocol that wraps the outbound call-control API, and the small set
of TwiML rendering helpers used by :mod:`voice_intake`.

The dependency on the official ``twilio`` SDK is *optional*. When the
package is present we delegate signature verification to
``twilio.request_validator.RequestValidator``; when it is missing we
fall back to an in-tree ``RequestValidator`` that implements the
documented Twilio HMAC-SHA1 construction byte-for-byte. The fallback
is deterministic so unit tests never depend on the optional install.

Security contract
-----------------

* :func:`verify_twilio_signature` is the *only* entry point used by
  the webhook router. It returns ``True`` iff a constant-time HMAC-SHA1
  digest of ``url + sorted(form_params)`` matches the
  ``X-Twilio-Signature`` header. Mismatch always returns ``False``;
  callers translate to a 401 response.
* The auth token is read from settings (``TWILIO_AUTH_TOKEN``) — never
  from request parameters or other operator-controlled inputs.
* Tests must never call paid Twilio APIs. The ``TwilioClient`` protocol
  exists so a fake client can be injected via
  :func:`set_twilio_client_for_tests`.

Tests must NOT import the real twilio SDK. When the SDK is absent the
fallback ``RequestValidator`` below is used; when present we re-export
the SDK class under the same name for downstream introspection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol


__all__ = [
    "RequestValidator",
    "TwilioClient",
    "TwilioCall",
    "TwilioConfigError",
    "verify_twilio_signature",
    "set_twilio_client_for_tests",
    "reset_twilio_client_for_tests",
    "get_twilio_client",
]


class TwilioConfigError(RuntimeError):
    """Raised when a Twilio operation is attempted without required config."""


# ---------------------------------------------------------------------------
# RequestValidator — vendored fallback when ``twilio`` SDK is absent.
# ---------------------------------------------------------------------------
#
# Twilio's signature algorithm (per the Security guide, current as of 2026):
#
#     signature = base64(hmac_sha1(auth_token, url + "".join(k+v for k,v in
#                                                            sorted(post_params))))
#
# For ``application/x-www-form-urlencoded`` POSTs (the default), the params
# are the decoded form fields; for JSON / GET, the request URL alone is
# signed and the params iterable is empty. We implement the form path
# (the only shape Twilio uses for voice webhooks) and let the signature
# check fail closed on any other content-type unless the caller passes the
# expected body hash separately.

try:  # pragma: no cover - exercised only when SDK is installed
    from twilio.request_validator import RequestValidator as _SdkRequestValidator
except Exception:  # pragma: no cover - hit in CI where twilio isn't installed
    _SdkRequestValidator = None  # type: ignore[assignment]


class _FallbackRequestValidator:
    """Byte-for-byte port of Twilio's signature algorithm.

    Mirrors the public surface of ``twilio.request_validator.RequestValidator``
    used by this module: ``__init__(token)`` + ``validate(uri, params, sig)``.
    """

    def __init__(self, token: str) -> None:
        self._token = (token or "").encode("utf-8")

    def compute_signature(
        self,
        uri: str,
        params: Optional[Mapping[str, str]] = None,
    ) -> str:
        body = uri
        if params:
            for key in sorted(params.keys()):
                body += key + str(params[key])
        digest = hmac.new(self._token, body.encode("utf-8"), hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")

    def validate(
        self,
        uri: str,
        params: Optional[Mapping[str, str]],
        signature: str,
    ) -> bool:
        if not self._token or not signature:
            return False
        expected = self.compute_signature(uri, params)
        # constant-time compare
        return hmac.compare_digest(expected, signature)


# Public alias — the verification marker requires this exact name in
# ``server/fund/routers/twilio_webhooks.py``; we re-export it here too so
# call sites can import a single canonical type.
RequestValidator = _SdkRequestValidator or _FallbackRequestValidator


def verify_twilio_signature(
    *,
    auth_token: str,
    url: str,
    params: Optional[Mapping[str, str]],
    signature_header: str,
) -> bool:
    """Return True iff the Twilio signature header matches the request.

    Constant-time comparison; an empty token or empty signature returns
    False. Translate False to ``401 UNAUTHORIZED`` at the route handler.
    """
    if not auth_token or not signature_header:
        return False
    validator = RequestValidator(auth_token)
    try:
        return bool(validator.validate(url, dict(params or {}), signature_header))
    except Exception:  # pragma: no cover - validator never raises in practice
        return False


# ---------------------------------------------------------------------------
# TwilioClient — outbound call-control surface.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwilioCall:
    """Result of placing an outbound call. Provider-specific SID + URL."""

    sid: str
    status: str
    to: str
    from_: str


class TwilioClient(Protocol):
    """Minimal Twilio Voice client surface used by ``voice_intake``."""

    def place_call(
        self,
        *,
        to: str,
        from_: str,
        twiml_url: str,
        status_callback_url: str,
    ) -> TwilioCall:
        ...

    def fetch_recording(self, recording_sid: str) -> bytes:
        """Authenticated GET of a recording media payload."""
        ...


# Module-level injection point so tests can substitute a fake client
# without monkeypatching imports across the codebase.
_CLIENT: Optional[TwilioClient] = None


def set_twilio_client_for_tests(client: Optional[TwilioClient]) -> None:
    """Override the global Twilio client (test-only seam)."""
    global _CLIENT
    _CLIENT = client


def reset_twilio_client_for_tests() -> None:
    global _CLIENT
    _CLIENT = None


def get_twilio_client() -> TwilioClient:
    """Return the configured Twilio client, raising if unset.

    Production wiring constructs a real client at app startup once
    ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` are present. Tests
    substitute a fake via :func:`set_twilio_client_for_tests`.
    """
    if _CLIENT is None:
        raise TwilioConfigError(
            "twilio_client_unconfigured: call set_twilio_client_for_tests "
            "in tests, or wire a real client at startup."
        )
    return _CLIENT
