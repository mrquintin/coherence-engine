# E-signature adapter spec (prompt 52, Wave 14)

## Purpose

Provide a single internal interface (`ESignatureProvider`) for sending
SAFE / term-sheet documents for signature through one of two
providers — DocuSign or Dropbox Sign (formerly HelloSign) — and
persisting the signed PDF artifact to object storage. The lifecycle
state machine, signature-verification rules, and document-storage
discipline are owned by the service layer; the backend modules are
swappable transport adapters with no business logic.

## NOT LEGAL ADVICE

The Jinja2 templates that ship under
`server/fund/data/legal_templates/` are **placeholders**. They exist
only so the e-signature pipeline is exercisable end-to-end in tests.

**The operator MUST replace each template with a version reviewed and
approved by their securities counsel before sending any production
signature request.** The Coherence Engine software does not produce
legal advice and does not warrant the legal sufficiency of any
document rendered from these templates. Disagreement between the
template and counsel-approved language is the operator's risk.

## Provider protocol

```python
class ESignatureProvider(Protocol):
    name: str
    def prepare(*, document, signers) -> None: ...
    def send(*, document, signers, idempotency_key) -> SendResponse: ...
    def void(*, provider_request_id, reason="") -> None: ...
    def fetch_signed_artifact(*, provider_request_id) -> SignedArtifact: ...
    def webhook_signature_ok(payload: bytes, headers: Mapping[str, str]) -> bool: ...
```

Two backends ship in tree:

| Backend | Module | Env vars |
|---|---|---|
| `DocuSignBackend` | `services/esignature_backends.py` | `DOCUSIGN_INTEGRATION_KEY`, `DOCUSIGN_USER_ID`, `DOCUSIGN_RSA_PRIVATE_KEY`, `DOCUSIGN_CONNECT_HMAC_SECRETS` (CSV) |
| `DropboxSignBackend` | `services/esignature_backends.py` | `DROPBOX_SIGN_API_KEY`, `DROPBOX_SIGN_API_BASE` (optional) |

In default-CI configuration both backends emit deterministic synthetic
ids (e.g. `env_<sha256-prefix>` for DocuSign envelopes, `sigreq_<sha256-prefix>`
for Dropbox Sign) so the service layer can be exercised under unit
tests without HTTP. The live HTTP paths are gated on a real API key
in the environment and exercised only in staging/prod.

## Lifecycle

```
prepared --(send)--> sent --(provider webhook)--> signed
                                       |--> declined
                                       |--> expired
                                       |--> voided
```

* `prepare` renders the document IN MEMORY, registers a row in
  `fund_signature_requests`, and discards the unsigned body. No
  network call is made.
* `send` re-renders the body (with the same template + variables),
  calls the provider's create-envelope / signature-request API, and
  advances the row to `sent`. The variable hash is asserted to
  match the prepare-time hash so the document body cannot be quietly
  substituted between the two calls.
* `void` cancels an in-flight request with the provider and writes
  `voided` locally.
* `apply_webhook` is invoked from the webhook routes. On `signed`
  it pulls the signed PDF from the provider and uploads it to object
  storage; the resulting `coh://` URI is stored in
  `signed_pdf_uri`. On any other terminal status it records the
  status and a completion timestamp.

## Webhook signature verification (mandatory)

Both webhook routes return HTTP 401 on signature failure and never
mutate state. There is no env-gated bypass and no dev-only skip
path. (Compare with `TWILIO_VALIDATE_WEBHOOK_SIGNATURE`, which
allows a dev-only opt-out for the Twilio voice surface — that
exception is NOT extended to e-signature.)

### DocuSign Connect HMAC v2

DocuSign computes `base64(HMAC-SHA-256(secret, raw_body))` and sends
the digest in `X-DocuSign-Signature-1` ... `X-DocuSign-Signature-10`
headers. Up to ten secrets can be active per account at once for
rotation; any matching pair (one secret + one header) verifies. The
verifier (`verify_docusign_webhook_signature`) iterates secrets ×
headers and returns `True` only when `hmac.compare_digest` matches a
non-empty pair.

### Dropbox Sign event-hash

Dropbox Sign computes `HMAC-SHA-256(api_key, event_time +
event_type)`, hex-encoded, and returns it in the JSON body at
`event.event_hash`. The verifier
(`verify_dropbox_sign_webhook_signature`) recomputes the digest from
the event time + type and constant-time-compares against the
delivered hash. The backend additionally accepts a raw-body HMAC
header (`X-DropboxSign-Signature`) for clients that prefer the HTTP
signature pattern.

## Endpoint table

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/webhooks/esignature/docusign` | DocuSign Connect HMAC v2 | 401 on bad sig; idempotent on duplicates |
| `POST` | `/webhooks/esignature/dropbox-sign` | Dropbox Sign event-hash | 401 on bad sig; idempotent on duplicates |

## Storage discipline (load-bearing prompt-52 prohibition)

The unsigned document body is never written to disk or to the
database. It exists in memory only between `render_template(...)` and
the provider's `send(...)` call. The database stores:

* `document_template` — template id (e.g. `safe_note_v1`);
* `template_vars_hash` — SHA-256 of the canonical-JSON
  serialization of the template variables (sorted keys, ASCII);
* `signed_pdf_uri` — `coh://` URI of the **signed** PDF the provider
  returned, uploaded via `services/object_storage.put`.

Reproducing the unsigned body from the database alone is therefore
impossible; reproduction requires the template file plus the original
variable map.

## Idempotency

* `prepare(idempotency_key=...)` collapses retries onto a single
  `SignatureRequest` row. The default key is
  `sha256("{application_id}|{template_id}|{salt}")` where the salt
  is typically a caller-provided request id.
* `apply_webhook` is idempotent on duplicates: when a webhook
  arrives for a row that is already in the same terminal status,
  the call is a no-op and `signed_pdf_uri` is preserved.

## Operator runbook

* Replace the placeholder templates under `server/fund/data/legal_templates/`
  with counsel-approved DOCX (or plain-text) Jinja2 files; bump the
  template id when changing the rendered body
  (e.g. `safe_note_v1` → `safe_note_v2`) so prior signed instruments
  remain reproducible.
* Configure `DOCUSIGN_CONNECT_HMAC_SECRETS` as a comma-separated list
  to support secret rotation without downtime.
* Set up the DocuSign Connect listener to point at
  `${PUBLIC_URL}/api/v1/webhooks/esignature/docusign` and the
  Dropbox Sign callback at
  `${PUBLIC_URL}/api/v1/webhooks/esignature/dropbox-sign`.
* Monitor `signed_pdf_uri == ''` rows in `sent` status that are
  older than the provider's expected signing window (~30 days for
  DocuSign default) — these are candidates for `void` followed by a
  fresh `prepare`.

## Tests

`tests/test_esignature.py` covers:

* template rendering + canonical-vars-hash determinism;
* backend prepare/send/fetch on the in-tree synthetic paths;
* webhook signature verification (valid, rotated secret, bad digest,
  empty inputs) for both providers;
* service-level idempotency (prepare + apply_webhook);
* signed-PDF round-trip through `LocalFilesystemBackend`;
* router via TestClient: 401 on bad signature, 200 + state mutation
  on valid webhook, 200 + no mutation on informational events.

Run with:

```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
  tests/test_esignature.py -v -p anyio -p asyncio
```

## Prohibitions honored

* Webhook signature verification is never skipped.
* Placeholder templates are documented as not counsel-reviewed; the
  README and this spec both state operator obligations clearly.
* The unsigned document body is never persisted — only the template
  id + the SHA-256 of its variables.
* No edits outside SCOPE.
