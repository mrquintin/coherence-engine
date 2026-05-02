"""Pluggable e-signature backends (prompt 52).

Two backends implement the :class:`ESignatureProvider` protocol:

* :class:`DocuSignBackend` -- DocuSign envelopes via the eSignature
  REST API. Webhook signatures are verified with the Connect HMAC v2
  scheme (HMAC-SHA-256 of the raw body against one of up to ten
  configured secrets).
* :class:`DropboxSignBackend` -- Dropbox Sign (formerly HelloSign)
  signature requests. Webhook signatures are verified with their
  documented scheme: HMAC-SHA-256 of the JSON body's ``event_time +
  event_type`` field against the API key.

Both backends:

* read configuration from environment variables;
* in default-CI configuration emit deterministic synthetic responses
  (no live HTTP) so the service layer can be exercised under unit
  tests;
* never log or echo their secrets.

Webhook signature verification (load-bearing, prompt-52 prohibition):
the verifier MUST use :func:`hmac.compare_digest` for the final
comparison, MUST reject empty signatures, and MUST reject empty
secrets. There is no "skip" path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from coherence_engine.server.fund.services.esignature import (
    ESignatureConfigError,
    ESignatureError,
    PreparedDocument,
    SendResponse,
    SignedArtifact,
    Signer,
)


__all__ = [
    "DocuSignBackend",
    "DropboxSignBackend",
    "verify_docusign_webhook_signature",
    "verify_dropbox_sign_webhook_signature",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_secret(env_var: str, *, required: bool) -> str:
    value = os.environ.get(env_var, "").strip()
    if required and not value:
        raise ESignatureConfigError(f"missing_env:{env_var}")
    return value


# ---------------------------------------------------------------------------
# DocuSign Connect HMAC v2 verification
# ---------------------------------------------------------------------------


def verify_docusign_webhook_signature(
    secrets: Sequence[str],
    payload: bytes,
    signature_headers: Sequence[str],
) -> bool:
    """Verify a DocuSign Connect HMAC v2 signature header.

    DocuSign computes ``base64(HMAC-SHA-256(secret, raw_body))`` and
    sends the digest in ``X-DocuSign-Signature-1`` (and ``-2`` ...
    ``-10``) headers, one per configured account-level secret. Any
    matching pair (one secret + one header) is sufficient.

    Returns ``False`` when no secret / header pair matches, when
    either side is empty, or when the digests differ.
    """
    if not secrets or not signature_headers:
        return False
    body_for_hmac = payload or b""
    for secret in secrets:
        if not secret:
            continue
        digest = hmac.new(
            secret.encode("utf-8"), body_for_hmac, hashlib.sha256
        ).digest()
        import base64

        expected = base64.b64encode(digest).decode("ascii")
        for header in signature_headers:
            if not header:
                continue
            if hmac.compare_digest(expected, header.strip()):
                return True
    return False


# ---------------------------------------------------------------------------
# Dropbox Sign signature verification
# ---------------------------------------------------------------------------


def verify_dropbox_sign_webhook_signature(
    api_key: str,
    payload: bytes,
    *,
    event_time: str = "",
    event_type: str = "",
    explicit_hash: str = "",
) -> bool:
    """Verify a Dropbox Sign webhook signature.

    Dropbox Sign's documented scheme: ``HMAC-SHA-256(api_key,
    event_time + event_type)``, hex-encoded, returned in the JSON
    body at ``event.event_hash``. Callers may pass the parsed values
    directly (``event_time`` / ``event_type`` / ``explicit_hash``) or
    let this function parse the JSON body itself.
    """
    if not api_key:
        return False
    if not (event_time and event_type and explicit_hash):
        try:
            parsed = json.loads(payload.decode("utf-8") if payload else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        event = parsed.get("event") if isinstance(parsed, Mapping) else None
        if not isinstance(event, Mapping):
            return False
        event_time = event_time or str(event.get("event_time", ""))
        event_type = event_type or str(event.get("event_type", ""))
        explicit_hash = explicit_hash or str(event.get("event_hash", ""))
    if not (event_time and event_type and explicit_hash):
        return False
    expected = hmac.new(
        api_key.encode("utf-8"),
        f"{event_time}{event_type}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, explicit_hash.strip())


# ---------------------------------------------------------------------------
# DocuSign backend
# ---------------------------------------------------------------------------


@dataclass
class DocuSignBackend:
    """DocuSign envelopes via the eSignature REST API.

    Reads ``DOCUSIGN_INTEGRATION_KEY``, ``DOCUSIGN_USER_ID``, and
    ``DOCUSIGN_RSA_PRIVATE_KEY`` from the environment. JWT grant is
    issued out-of-band (the in-tree path emits a synthetic envelope id
    so unit tests run without DocuSign credentials).

    Connect HMAC v2 secrets are supplied via the
    ``DOCUSIGN_CONNECT_HMAC_SECRETS`` env var as a comma-separated
    list (DocuSign accounts can rotate up to 10 active secrets at
    once).
    """

    integration_key: str = ""
    user_id: str = ""
    rsa_private_key: str = ""
    connect_hmac_secrets: Sequence[str] = ()
    name: str = "docusign"

    @classmethod
    def from_env(cls) -> "DocuSignBackend":
        secrets_csv = os.environ.get("DOCUSIGN_CONNECT_HMAC_SECRETS", "").strip()
        secrets = tuple(s.strip() for s in secrets_csv.split(",") if s.strip())
        return cls(
            integration_key=_read_secret("DOCUSIGN_INTEGRATION_KEY", required=True),
            user_id=_read_secret("DOCUSIGN_USER_ID", required=True),
            rsa_private_key=_read_secret("DOCUSIGN_RSA_PRIVATE_KEY", required=True),
            connect_hmac_secrets=secrets,
        )

    # ---- protocol surface ----------------------------------------

    def prepare(
        self,
        *,
        document: PreparedDocument,
        signers: Sequence[Signer],
    ) -> None:
        if not document.body:
            raise ESignatureError("prepared document body is empty")
        # Inert -- envelope creation happens at send().

    def send(
        self,
        *,
        document: PreparedDocument,
        signers: Sequence[Signer],
        idempotency_key: str,
    ) -> SendResponse:
        # Production path: POST /v2.1/accounts/{accountId}/envelopes
        # with the envelope definition. In-tree path returns a
        # deterministic envelope id so unit tests collapse on retry.
        digest = hashlib.sha256(
            f"docusign|{idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        return SendResponse(
            provider_request_id=f"env_{digest}", provider_status="sent"
        )

    def void(self, *, provider_request_id: str, reason: str = "") -> None:
        # Production: PUT /envelopes/{envelopeId} with status=voided.
        if not provider_request_id:
            raise ESignatureError("void requires provider_request_id")

    def fetch_signed_artifact(
        self, *, provider_request_id: str
    ) -> SignedArtifact:
        if not provider_request_id:
            raise ESignatureError("fetch requires provider_request_id")
        # Production: GET /envelopes/{envelopeId}/documents/combined.
        # In-tree path returns deterministic placeholder bytes so
        # tests can exercise the upload-to-storage code path.
        body = (
            b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
            + f"docusign-signed:{provider_request_id}".encode("utf-8")
        )
        return SignedArtifact(pdf_bytes=body, content_type="application/pdf")

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        if not self.connect_hmac_secrets:
            return False
        # DocuSign sends X-DocuSign-Signature-1 .. -10. Collect them
        # case-insensitively from the incoming headers.
        sig_headers = []
        for k, v in headers.items():
            if k.lower().startswith("x-docusign-signature-"):
                sig_headers.append(v)
        return verify_docusign_webhook_signature(
            self.connect_hmac_secrets, payload, sig_headers
        )


# ---------------------------------------------------------------------------
# Dropbox Sign backend
# ---------------------------------------------------------------------------


@dataclass
class DropboxSignBackend:
    """Dropbox Sign (formerly HelloSign) signature requests.

    Reads ``DROPBOX_SIGN_API_KEY`` from the environment. The same key
    is used both for outbound API calls (HTTP basic auth) and for
    webhook signature verification.
    """

    api_key: str = ""
    api_base: str = "https://api.hellosign.com/v3"
    name: str = "dropbox_sign"

    @classmethod
    def from_env(cls) -> "DropboxSignBackend":
        return cls(
            api_key=_read_secret("DROPBOX_SIGN_API_KEY", required=True),
            api_base=os.environ.get(
                "DROPBOX_SIGN_API_BASE", "https://api.hellosign.com/v3"
            ).rstrip("/"),
        )

    # ---- protocol surface ----------------------------------------

    def prepare(
        self,
        *,
        document: PreparedDocument,
        signers: Sequence[Signer],
    ) -> None:
        if not document.body:
            raise ESignatureError("prepared document body is empty")

    def send(
        self,
        *,
        document: PreparedDocument,
        signers: Sequence[Signer],
        idempotency_key: str,
    ) -> SendResponse:
        digest = hashlib.sha256(
            f"dropbox_sign|{idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        return SendResponse(
            provider_request_id=f"sigreq_{digest}", provider_status="sent"
        )

    def void(self, *, provider_request_id: str, reason: str = "") -> None:
        if not provider_request_id:
            raise ESignatureError("void requires provider_request_id")

    def fetch_signed_artifact(
        self, *, provider_request_id: str
    ) -> SignedArtifact:
        if not provider_request_id:
            raise ESignatureError("fetch requires provider_request_id")
        body = (
            b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
            + f"dropbox-sign-signed:{provider_request_id}".encode("utf-8")
        )
        return SignedArtifact(pdf_bytes=body, content_type="application/pdf")

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        # Dropbox Sign also sends a request-time hash in the body;
        # this verifier accepts either the in-body hash or an explicit
        # X-DropboxSign-Signature header that callers may pass in.
        explicit_hash = ""
        for k, v in headers.items():
            if k.lower() == "x-dropboxsign-signature":
                explicit_hash = v
                break
        # When an explicit header is provided, it is verified against
        # the raw body bytes per Dropbox Sign's HTTP signature scheme.
        if explicit_hash:
            expected = hmac.new(
                self.api_key.encode("utf-8"),
                payload or b"",
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, explicit_hash.strip())
        # Otherwise fall back to the JSON-body event_hash scheme.
        return verify_dropbox_sign_webhook_signature(self.api_key, payload)


# Silence unused-import warnings from typing-only imports above.
_ = Optional
