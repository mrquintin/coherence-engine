"""E-signature service (prompt 52).

Wraps a pluggable :class:`ESignatureProvider` (DocuSign / Dropbox Sign)
behind a small service that owns the SAFE / term-sheet lifecycle:

    prepare --(send)--> sent --(provider webhook)--> signed
                                          |--> declined
                                          |--> expired
                                          |--> voided

Storage discipline (load-bearing prompt-52 prohibition)
-------------------------------------------------------

The unsigned document body is rendered from a Jinja2 template in
memory and discarded after the provider acknowledges the send. The
database stores only:

* ``document_template`` -- template id (e.g. ``safe_note_v1``);
* ``template_vars_hash`` -- SHA-256 of the canonicalized variables;
* ``signed_pdf_uri`` -- object-storage URI of the *signed* PDF the
  provider returned (uploaded via :mod:`object_storage`).

Reproducing the exact unsigned body therefore requires the template
file plus the original variable map; nothing in the database alone
leaks the document's text.

Operator obligation
-------------------

Templates ship as **placeholders** (see
``server/fund/data/legal_templates/README.md``). They MUST be
reviewed by securities counsel before any production signature
request. This software does not provide legal advice.

Webhook signature verification
------------------------------

Webhooks from both providers MUST pass
:func:`ESignatureProvider.webhook_signature_ok` before any state
transition. The router (``routers/esignature_webhooks.py``) returns
HTTP 401 on failure and never mutates state -- mirroring the Twilio /
Stripe pattern used elsewhere in the codebase.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.services import object_storage as _object_storage


__all__ = [
    "ESignatureProvider",
    "ESignatureError",
    "ESignatureConfigError",
    "ESignatureService",
    "PreparedDocument",
    "SendResponse",
    "SignedArtifact",
    "Signer",
    "ALLOWED_STATUSES",
    "TERMINAL_STATUSES",
    "render_template",
    "compute_template_vars_hash",
    "compute_idempotency_key",
    "load_template_path",
]


_LOG = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


ALLOWED_STATUSES = frozenset(
    {"prepared", "sent", "signed", "declined", "expired", "voided"}
)
TERMINAL_STATUSES = frozenset({"signed", "declined", "expired", "voided"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ESignatureError(Exception):
    """Raised by the e-signature service / backends on failure."""


class ESignatureConfigError(ESignatureError):
    """Raised when required env vars for a backend are missing."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signer:
    """A single signer on a signature request."""

    name: str
    email: str
    role: str = "signer"


@dataclass(frozen=True)
class PreparedDocument:
    """Output of :func:`render_template` -- the rendered document body
    plus a content-addressable identity (template id + vars hash).

    The ``body`` field is in-memory only; it MUST NOT be persisted
    by callers.
    """

    template_id: str
    body: bytes
    vars_hash: str
    content_type: str = "text/plain"


@dataclass(frozen=True)
class SendResponse:
    """Result of :meth:`ESignatureProvider.send`."""

    provider_request_id: str
    provider_status: str = "sent"


@dataclass(frozen=True)
class SignedArtifact:
    """Bytes returned by :meth:`ESignatureProvider.fetch_signed_artifact`."""

    pdf_bytes: bytes
    content_type: str = "application/pdf"


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ESignatureProvider(Protocol):
    """Backend transport contract for an e-signature provider.

    Implementations MUST:

    * expose a ``name`` string (``"docusign"`` or ``"dropbox_sign"``);
    * implement ``prepare`` -- inert local registration of the
      envelope-to-be (no network call);
    * implement ``send`` -- returns the upstream's request id;
    * implement ``void`` -- cancels an in-flight request;
    * implement ``fetch_signed_artifact`` -- returns the signed PDF
      bytes. Idempotent: callers may invoke this multiple times for
      the same request id.
    * implement ``webhook_signature_ok(payload, headers)`` -- returns
      True only when the upstream's signature header verifies against
      the configured secret.
    """

    name: str

    def prepare(
        self,
        *,
        document: PreparedDocument,
        signers: Sequence[Signer],
    ) -> None:  # pragma: no cover - protocol
        ...

    def send(
        self,
        *,
        document: PreparedDocument,
        signers: Sequence[Signer],
        idempotency_key: str,
    ) -> SendResponse:  # pragma: no cover - protocol
        ...

    def void(
        self, *, provider_request_id: str, reason: str = ""
    ) -> None:  # pragma: no cover - protocol
        ...

    def fetch_signed_artifact(
        self, *, provider_request_id: str
    ) -> SignedArtifact:  # pragma: no cover - protocol
        ...

    def webhook_signature_ok(
        self, payload: bytes, headers: Mapping[str, str]
    ) -> bool:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "legal_templates"
)


def load_template_path(template_id: str) -> Path:
    """Resolve a template id (e.g. ``safe_note_v1``) to its on-disk path.

    The template id is a slug; the on-disk file is
    ``<id>.docx.j2``. Raises :class:`ESignatureError` for unknown ids.
    """
    if not template_id or not template_id.replace("_", "").replace("-", "").isalnum():
        raise ESignatureError(f"invalid template id: {template_id!r}")
    path = _TEMPLATE_DIR / f"{template_id}.docx.j2"
    if not path.is_file():
        raise ESignatureError(f"template not found: {template_id}")
    return path


def _canonical_vars_json(template_vars: Mapping[str, Any]) -> str:
    """Stable JSON serialization for hashing -- sorted keys + ASCII."""
    return json.dumps(template_vars, sort_keys=True, ensure_ascii=True, default=str)


def compute_template_vars_hash(template_vars: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonical-JSON serialization of vars."""
    return hashlib.sha256(
        _canonical_vars_json(template_vars).encode("utf-8")
    ).hexdigest()


def render_template(
    template_id: str, template_vars: Mapping[str, Any]
) -> PreparedDocument:
    """Render a template to bytes IN MEMORY and return the result.

    Uses Jinja2 if available; otherwise falls back to a small,
    explicit ``{{ var }}`` substitutor that supports the simple
    placeholder grammar in the shipped placeholder template. The
    fallback exists so that the e-signature pipeline can be exercised
    in CI environments that don't ship Jinja2.

    The returned :class:`PreparedDocument`'s ``body`` is in-memory
    only and MUST NOT be persisted to the database.
    """
    path = load_template_path(template_id)
    raw = path.read_text(encoding="utf-8")
    body = _render_with_jinja_or_fallback(raw, template_vars)
    return PreparedDocument(
        template_id=template_id,
        body=body.encode("utf-8"),
        vars_hash=compute_template_vars_hash(template_vars),
        content_type="text/plain",
    )


def _render_with_jinja_or_fallback(
    raw: str, template_vars: Mapping[str, Any]
) -> str:
    try:
        import jinja2  # type: ignore

        env = jinja2.Environment(
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
        )
        return env.from_string(raw).render(**template_vars)
    except ImportError:
        # Minimal ``{{ name }}`` substitution. Strips ``{# ... #}``
        # comments first so the placeholder file's preamble does not
        # leak into the rendered output.
        import re

        without_comments = re.sub(r"\{#.*?#\}", "", raw, flags=re.DOTALL)

        def _sub(match: "re.Match[str]") -> str:
            key = match.group(1).strip()
            if key not in template_vars:
                raise ESignatureError(f"missing template var: {key}")
            return str(template_vars[key])

        return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", _sub, without_comments)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def compute_idempotency_key(
    application_id: str, template_id: str, salt: str
) -> str:
    """Deterministic idempotency key for ``prepare``.

    A retry of the same logical prepare with the same salt collapses
    onto one ``SignatureRequest`` row; a fresh prepare uses a new
    salt.
    """
    payload = f"{application_id}|{template_id}|{salt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class ESignatureService:
    """Owns the SignatureRequest lifecycle.

    Backends are injected via the constructor (or selected at
    call-time via the ``provider`` argument) -- the service itself
    has no knowledge of upstream HTTP shapes.
    """

    db: Session
    storage: Any = field(default=None)

    def __post_init__(self) -> None:
        if self.storage is None:
            self.storage = _object_storage.get_object_storage()

    # ----- prepare -------------------------------------------------

    def prepare(
        self,
        *,
        provider: ESignatureProvider,
        application_id: str,
        document_template: str,
        template_vars: Mapping[str, Any],
        signers: Sequence[Signer],
        idempotency_key: Optional[str] = None,
    ) -> models.SignatureRequest:
        """Render the template in memory, register the intent, return row.

        No network call is made and the unsigned body is discarded
        after :meth:`PreparedDocument` returns. The provider's
        :meth:`prepare` hook is invoked so backends may pre-validate
        the document, but no envelope is created upstream.
        """
        if not signers:
            raise ESignatureError("at least one signer required")
        document = render_template(document_template, template_vars)
        key = idempotency_key or compute_idempotency_key(
            application_id, document_template, salt=uuid.uuid4().hex
        )
        existing = (
            self.db.query(models.SignatureRequest)
            .filter(models.SignatureRequest.idempotency_key == key)
            .one_or_none()
        )
        if existing is not None:
            return existing

        provider.prepare(document=document, signers=signers)
        row = models.SignatureRequest(
            id=f"sig_{uuid.uuid4().hex[:32]}",
            application_id=application_id,
            document_template=document_template,
            template_vars_hash=document.vars_hash,
            provider=provider.name,
            provider_request_id="",
            status="prepared",
            signed_pdf_uri="",
            signers_json=json.dumps(
                [{"name": s.name, "email": s.email, "role": s.role} for s in signers]
            ),
            idempotency_key=key,
            created_at=_utc_now(),
        )
        self.db.add(row)
        self.db.flush()
        # The unsigned body is now out of scope; nothing else holds
        # a reference. It is never written to disk or db.
        del document
        return row

    # ----- send ----------------------------------------------------

    def send(
        self,
        *,
        provider: ESignatureProvider,
        request: models.SignatureRequest,
        template_vars: Mapping[str, Any],
    ) -> models.SignatureRequest:
        """Re-render the body, dispatch to provider, advance to ``sent``.

        Re-rendering at send time avoids persisting the unsigned body
        between prepare and send. The vars hash is asserted to match
        the row's stored hash so a caller cannot quietly substitute
        different variables between prepare and send.
        """
        if request.status not in {"prepared", "sent"}:
            raise ESignatureError(
                f"cannot send from status={request.status}"
            )
        document = render_template(request.document_template, template_vars)
        if document.vars_hash != request.template_vars_hash:
            raise ESignatureError(
                "template_vars_hash mismatch -- variables changed since prepare"
            )
        signers = self._signers_from_row(request)
        response = provider.send(
            document=document,
            signers=signers,
            idempotency_key=request.idempotency_key,
        )
        request.provider_request_id = response.provider_request_id
        request.status = "sent"
        request.sent_at = _utc_now()
        self.db.flush()
        del document
        return request

    # ----- void ----------------------------------------------------

    def void(
        self,
        *,
        provider: ESignatureProvider,
        request: models.SignatureRequest,
        reason: str = "",
    ) -> models.SignatureRequest:
        if request.status in TERMINAL_STATUSES:
            raise ESignatureError(
                f"cannot void from terminal status={request.status}"
            )
        if request.provider_request_id:
            provider.void(
                provider_request_id=request.provider_request_id,
                reason=reason,
            )
        request.status = "voided"
        request.completed_at = _utc_now()
        self.db.flush()
        return request

    # ----- webhook reconciliation ---------------------------------

    def apply_webhook(
        self,
        *,
        provider: ESignatureProvider,
        provider_request_id: str,
        new_status: str,
    ) -> Optional[models.SignatureRequest]:
        """Advance a SignatureRequest in response to a verified webhook.

        Idempotent: a duplicate webhook for an already-terminal row
        is a no-op. ``signed`` triggers the
        :meth:`ESignatureProvider.fetch_signed_artifact` call and a
        put to object storage.
        """
        if new_status not in ALLOWED_STATUSES:
            raise ESignatureError(f"unknown webhook status: {new_status}")
        row = (
            self.db.query(models.SignatureRequest)
            .filter(
                models.SignatureRequest.provider_request_id
                == provider_request_id
            )
            .one_or_none()
        )
        if row is None:
            return None
        if row.status in TERMINAL_STATUSES and row.status == new_status:
            return row
        if new_status == "signed":
            artifact = provider.fetch_signed_artifact(
                provider_request_id=provider_request_id
            )
            put = self.storage.put(
                f"signatures/{row.id}/signed.pdf",
                artifact.pdf_bytes,
                content_type=artifact.content_type,
            )
            row.signed_pdf_uri = put.uri
        row.status = new_status
        if new_status in TERMINAL_STATUSES:
            row.completed_at = _utc_now()
        self.db.flush()
        return row

    # ----- helpers -------------------------------------------------

    def _signers_from_row(
        self, row: models.SignatureRequest
    ) -> Sequence[Signer]:
        try:
            raw = json.loads(row.signers_json or "[]")
        except json.JSONDecodeError as exc:
            raise ESignatureError(f"corrupt signers_json on {row.id}") from exc
        return [
            Signer(
                name=str(s.get("name", "")),
                email=str(s.get("email", "")),
                role=str(s.get("role", "signer")),
            )
            for s in raw
        ]


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def provider_from_env(name: str) -> ESignatureProvider:
    """Build a backend instance from environment variables.

    Lazy import avoids a hard dependency on either provider's SDK at
    module-load time.
    """
    normalized = (name or "").strip().lower()
    from coherence_engine.server.fund.services import esignature_backends as backends

    if normalized == "docusign":
        return backends.DocuSignBackend.from_env()
    if normalized in {"dropbox_sign", "dropboxsign", "hellosign"}:
        return backends.DropboxSignBackend.from_env()
    raise ESignatureConfigError(f"unsupported esignature provider: {name!r}")


# Silence unused-import linters in environments that lack ``os``-based
# dynamic dispatch (factory above is the only consumer).
_ = os
