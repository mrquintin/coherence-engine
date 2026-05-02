"""Pluggable CRM backends (prompt 55).

Two backends implement the :class:`CRMBackend` protocol:

* :class:`AffinityBackend` -- Affinity (primary) is reached via its
  REST API. Affinity webhooks are signed with HMAC-SHA-256 of the raw
  body using the configured signing key, returned in the
  ``Affinity-Webhook-Signature`` header (hex digest).
* :class:`HubSpotBackend` -- HubSpot (alternate) is reached via the
  Private App token. HubSpot webhooks are signed with HMAC-SHA-256 of
  the raw body using the app secret, returned in the
  ``X-HubSpot-Signature-v3`` header (base64 digest).

Both backends:

* read configuration from environment variables;
* in default-CI configuration emit deterministic synthetic responses
  (no live HTTP) so the service layer can be exercised under unit
  tests;
* never log or echo their secrets.

Webhook signature verification (load-bearing prompt-55 prohibition):
the verifier MUST use :func:`hmac.compare_digest` for the final
comparison, MUST reject empty signatures, and MUST reject empty
secrets. There is no "skip" path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence


__all__ = [
    "CRMBackend",
    "CRMBackendError",
    "CRMConfigError",
    "CRMUpdate",
    "AffinityBackend",
    "HubSpotBackend",
    "verify_affinity_webhook_signature",
    "verify_hubspot_webhook_signature",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CRMBackendError(Exception):
    """Raised by CRM backends on transport / API failure."""


class CRMConfigError(CRMBackendError):
    """Raised when required env vars for a backend are missing."""


# ---------------------------------------------------------------------------
# Inbound update value type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CRMUpdate:
    """Normalized inbound update from any CRM backend.

    A :class:`CRMUpdate` is the parsed, vendor-agnostic representation
    of what arrived in a webhook. The service layer applies it via
    :func:`apply_inbound_update` so the CRM never reaches the database
    directly.

    ``provider`` -- ``"affinity"`` or ``"hubspot"``.
    ``external_id`` -- the CRM's id for the deal/opportunity.
    ``application_id`` -- our local application id, when the backend
    can resolve it (HubSpot custom property, Affinity field).
    ``founder_email`` -- fallback resolution path when
    ``application_id`` is absent.
    ``tags`` -- list of partner-applied tags (replaces local tags
    last-writer-wins).
    ``notes`` -- list of partner notes (appended last-writer-wins).
    ``deal_stage`` -- partner deal-stage label, never mapped onto
    ``Decision.verdict``.
    ``occurred_at`` -- vendor-supplied event timestamp (ISO-8601).
    ``raw`` -- full parsed JSON, retained for trace/debug; callers
    SHOULD NOT mutate state directly from this.
    """

    provider: str
    external_id: str
    application_id: str = ""
    founder_email: str = ""
    tags: Sequence[str] = ()
    notes: Sequence[str] = ()
    deal_stage: str = ""
    occurred_at: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_secret(env_var: str, *, required: bool) -> str:
    value = os.environ.get(env_var, "").strip()
    if required and not value:
        raise CRMConfigError(f"missing_env:{env_var}")
    return value


def _coerce_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [value.strip()]
    return []


# ---------------------------------------------------------------------------
# Affinity webhook verification
# ---------------------------------------------------------------------------


def verify_affinity_webhook_signature(
    secret: str,
    payload: bytes,
    signature_header: str,
) -> bool:
    """Verify an Affinity webhook signature.

    Affinity computes ``hex(HMAC-SHA-256(secret, raw_body))`` and
    sends the digest in the ``Affinity-Webhook-Signature`` header
    (commonly prefixed ``sha256=``). Returns ``False`` when either
    side is empty or when the digests differ.
    """
    if not secret or not signature_header:
        return False
    candidate = signature_header.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate[len("sha256="):]
    expected = hmac.new(
        secret.encode("utf-8"), payload or b"", hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, candidate)


# ---------------------------------------------------------------------------
# HubSpot webhook verification
# ---------------------------------------------------------------------------


def verify_hubspot_webhook_signature(
    secret: str,
    payload: bytes,
    signature_header: str,
) -> bool:
    """Verify a HubSpot v3 webhook signature.

    HubSpot v3 computes
    ``base64(HMAC-SHA-256(secret, raw_body))`` and sends the digest in
    ``X-HubSpot-Signature-v3``. Returns ``False`` when either side is
    empty or when the digests differ.
    """
    if not secret or not signature_header:
        return False
    digest = hmac.new(
        secret.encode("utf-8"), payload or b"", hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature_header.strip())


# ---------------------------------------------------------------------------
# Affinity backend
# ---------------------------------------------------------------------------


@dataclass
class AffinityBackend:
    """Affinity REST API backend.

    Reads ``AFFINITY_API_KEY`` and ``AFFINITY_WEBHOOK_SECRET`` from
    the environment. The unit-test path emits deterministic synthetic
    upstream ids (``aff_*``) so callers can exercise the service
    layer without any live HTTP.
    """

    api_key: str = ""
    webhook_secret: str = ""
    api_base: str = "https://api.affinity.co"
    name: str = "affinity"

    @classmethod
    def from_env(cls) -> "AffinityBackend":
        return cls(
            api_key=_read_secret("AFFINITY_API_KEY", required=True),
            webhook_secret=_read_secret(
                "AFFINITY_WEBHOOK_SECRET", required=False
            ),
            api_base=os.environ.get(
                "AFFINITY_API_BASE", "https://api.affinity.co"
            ).rstrip("/"),
        )

    # ---- protocol surface ----------------------------------------

    def upsert_founder(
        self,
        *,
        founder_id: str,
        full_name: str,
        email: str,
        company_name: str,
    ) -> str:
        if not founder_id:
            raise CRMBackendError("upsert_founder requires founder_id")
        digest = hashlib.sha256(
            f"affinity|founder|{founder_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"aff_person_{digest}"

    def upsert_application(
        self,
        *,
        application_id: str,
        founder_id: str,
        status: str,
        verdict: str = "",
        one_liner: str = "",
        requested_check_usd: int = 0,
    ) -> str:
        if not application_id:
            raise CRMBackendError(
                "upsert_application requires application_id"
            )
        digest = hashlib.sha256(
            f"affinity|app|{application_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"aff_opp_{digest}"

    def verify_webhook(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        if not self.webhook_secret:
            return False
        sig = ""
        for key, value in headers.items():
            if key.lower() == "affinity-webhook-signature":
                sig = value
                break
        return verify_affinity_webhook_signature(
            self.webhook_secret, payload, sig
        )

    def parse_webhook(self, body: bytes) -> Optional[CRMUpdate]:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        # Affinity wraps the change in ``{"type": ..., "body": ...}``.
        body_obj = payload.get("body") if isinstance(payload, Mapping) else None
        if not isinstance(body_obj, Mapping):
            body_obj = payload
        external_id = str(
            body_obj.get("opportunity_id")
            or body_obj.get("entity_id")
            or body_obj.get("id")
            or ""
        )
        if not external_id:
            return None
        fields = body_obj.get("fields") if isinstance(body_obj, Mapping) else {}
        if not isinstance(fields, Mapping):
            fields = {}
        application_id = str(
            fields.get("application_id")
            or body_obj.get("application_id")
            or ""
        )
        founder_email = str(
            fields.get("founder_email")
            or body_obj.get("founder_email")
            or ""
        )
        tags = _coerce_str_list(body_obj.get("tags"))
        notes = _coerce_str_list(body_obj.get("notes"))
        deal_stage = str(
            body_obj.get("stage")
            or body_obj.get("list_entry_stage")
            or fields.get("stage", "")
        )
        occurred_at = str(
            payload.get("created_at")
            or body_obj.get("changed_at")
            or ""
        )
        return CRMUpdate(
            provider=self.name,
            external_id=external_id,
            application_id=application_id,
            founder_email=founder_email,
            tags=tags,
            notes=notes,
            deal_stage=deal_stage,
            occurred_at=occurred_at,
            raw=dict(payload),
        )

    def fetch_recent_updates(
        self, *, since_iso: str
    ) -> Sequence[CRMUpdate]:
        """Synthetic delta feed (in-tree path).

        In production this would issue ``GET /opportunities?updated_since=``.
        The unit-test path returns an empty sequence; tests inject
        deltas directly into the reconciliation routine.
        """
        if not since_iso:
            raise CRMBackendError("fetch_recent_updates requires since_iso")
        return ()


# ---------------------------------------------------------------------------
# HubSpot backend
# ---------------------------------------------------------------------------


@dataclass
class HubSpotBackend:
    """HubSpot Private App backend.

    Reads ``HUBSPOT_PRIVATE_APP_TOKEN`` and ``HUBSPOT_WEBHOOK_SECRET``
    from the environment. The unit-test path emits deterministic
    synthetic upstream ids (``hs_*``).
    """

    private_app_token: str = ""
    webhook_secret: str = ""
    api_base: str = "https://api.hubapi.com"
    name: str = "hubspot"

    @classmethod
    def from_env(cls) -> "HubSpotBackend":
        return cls(
            private_app_token=_read_secret(
                "HUBSPOT_PRIVATE_APP_TOKEN", required=True
            ),
            webhook_secret=_read_secret(
                "HUBSPOT_WEBHOOK_SECRET", required=False
            ),
            api_base=os.environ.get(
                "HUBSPOT_API_BASE", "https://api.hubapi.com"
            ).rstrip("/"),
        )

    # ---- protocol surface ----------------------------------------

    def upsert_founder(
        self,
        *,
        founder_id: str,
        full_name: str,
        email: str,
        company_name: str,
    ) -> str:
        if not founder_id:
            raise CRMBackendError("upsert_founder requires founder_id")
        digest = hashlib.sha256(
            f"hubspot|founder|{founder_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"hs_contact_{digest}"

    def upsert_application(
        self,
        *,
        application_id: str,
        founder_id: str,
        status: str,
        verdict: str = "",
        one_liner: str = "",
        requested_check_usd: int = 0,
    ) -> str:
        if not application_id:
            raise CRMBackendError(
                "upsert_application requires application_id"
            )
        digest = hashlib.sha256(
            f"hubspot|app|{application_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"hs_deal_{digest}"

    def verify_webhook(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:
        if not self.webhook_secret:
            return False
        sig = ""
        for key, value in headers.items():
            if key.lower() == "x-hubspot-signature-v3":
                sig = value
                break
        return verify_hubspot_webhook_signature(
            self.webhook_secret, payload, sig
        )

    def parse_webhook(self, body: bytes) -> Optional[CRMUpdate]:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        # HubSpot delivers an array of events OR a single event dict.
        event: Mapping[str, Any]
        if isinstance(payload, list):
            if not payload:
                return None
            event = payload[0] if isinstance(payload[0], Mapping) else {}
        elif isinstance(payload, Mapping):
            event = payload
        else:
            return None

        properties = event.get("properties")
        if not isinstance(properties, Mapping):
            properties = {}
        external_id = str(
            event.get("objectId")
            or event.get("dealId")
            or event.get("id")
            or ""
        )
        if not external_id:
            return None
        application_id = str(
            properties.get("application_id")
            or event.get("application_id")
            or ""
        )
        founder_email = str(
            properties.get("email")
            or event.get("email")
            or ""
        )
        tags = _coerce_str_list(properties.get("tags"))
        notes = _coerce_str_list(properties.get("notes"))
        deal_stage = str(
            properties.get("dealstage")
            or event.get("dealstage")
            or ""
        )
        occurred_at = str(
            event.get("occurredAt")
            or event.get("occurred_at")
            or ""
        )
        return CRMUpdate(
            provider=self.name,
            external_id=external_id,
            application_id=application_id,
            founder_email=founder_email,
            tags=tags,
            notes=notes,
            deal_stage=deal_stage,
            occurred_at=occurred_at,
            raw=payload if isinstance(payload, Mapping) else {"events": payload},
        )

    def fetch_recent_updates(
        self, *, since_iso: str
    ) -> Sequence[CRMUpdate]:
        if not since_iso:
            raise CRMBackendError("fetch_recent_updates requires since_iso")
        return ()


# ---------------------------------------------------------------------------
# Backend protocol (structural, not nominal)
# ---------------------------------------------------------------------------


class CRMBackend:
    """Marker protocol for CRM backends.

    Any class that exposes ``upsert_founder``, ``upsert_application``,
    ``verify_webhook``, ``parse_webhook``, and ``fetch_recent_updates``
    is treated as a CRM backend. Both :class:`AffinityBackend` and
    :class:`HubSpotBackend` satisfy the contract structurally; this
    class exists primarily for type hints.
    """

    name: str
