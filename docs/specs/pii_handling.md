# PII handling — tokenization + audit-log RLS (prompt 58)

This document covers how the fund backend stores, exposes, and audits
sensitive personal data: email, full legal name, phone, residence
address. It is the operator-facing reference for the
`pii_tokenization` and `pii_clear_audit` modules and for the
`pii_clear_audit_log` table installed by migration `20260425_000015`.

## Storage shape

For every PII field the on-disk schema is a triple:

| column                    | purpose                                                                |
| ------------------------- | ---------------------------------------------------------------------- |
| `<field>_token`           | deterministic HMAC-SHA-256 token under the per-tenant salt             |
| `<field>_clear`           | per-row AES-GCM ciphertext of the clear value (prompt 57 encryption)   |
| `<field>_clear_key_id`    | foreign reference into `fund_encryption_keys`                          |

The `<field>_token` form is what every downstream system (CRM, dedup,
analytics, internal joins) consumes. The `<field>_clear` form is
reachable only through the helpers in
`server/fund/services/pii_clear_audit.py`.

For the email field the migration also leaves the legacy `email`
column in place; it is the **expand** half of an
expand-backfill-contract rollout. A future prompt will drop `email`
once every caller is on `email_token` + `read_clear_email`.

## Tokenization invariant

`tokenize(value, kind=..., tenant_salt=...)` returns

    "tok_" + kind + "_" + first_16_bytes_hex(HMAC_SHA256(salt, kind + ":" + value))

Properties this guarantees:

* **Deterministic per tenant.** The same `(value, kind, salt)` triple
  always produces the same token. Equality joins, dedup, and CRM
  upserts continue to work without the system ever loading clear PII.
* **Non-reversible without the salt.** HMAC-SHA-256 has no usable
  preimage attack and the 128-bit truncation does not weaken
  preimage resistance below the salt's entropy floor.
* **Domain-separated by kind.** A phone number and an email that
  happen to share a string never produce colliding tokens because
  `kind` is mixed into the HMAC input.
* **Email normalisation.** Email values are lower-cased and stripped
  before tokenization so capitalisation drift in upstream sources
  does not produce two tokens for the same person.

Empty / whitespace-only values raise `ValueError` rather than being
tokenized into a global "empty" token (which would leak the cardinality
of empty fields across tenants).

## Salt management

The salt lives in the secret manager under the name `PII_TENANT_SALT`.
Resolution order at tokenize time:

1. Explicit `tenant_salt=` keyword argument (used in tests and in
   re-tokenization migrations).
2. `PII_TENANT_SALT` env var (operator can override per-process).
3. The runtime `SecretManager.get(...)` path — typically backed by
   Doppler / Vault / Supabase Vault per the deployment.

If none of the three sources resolves a salt, `tokenize` raises
`PIITokenizationError` and the caller MUST surface a 500-class error
rather than fall back to clear-text storage.

### Salt rotation play (operator-only)

Rotating `PII_TENANT_SALT` invalidates **every existing token** because
the HMAC output changes. Rotation therefore runs as a documented play,
not an ad-hoc env edit:

1. Mint the new salt and store it in the secret manager under a
   versioned alias (`PII_TENANT_SALT_v2`).
2. Run the re-tokenization migration: for every row holding a
   `<field>_token`, decrypt `<field>_clear` under the existing
   per-row key, recompute the token under the new salt, and write
   the new value back into `<field>_token`. The clear ciphertext is
   untouched.
3. Atomically promote `PII_TENANT_SALT_v2` → `PII_TENANT_SALT` and
   restart the application tier so the in-process resolver picks up
   the new value.
4. Retire the old salt only after every downstream system that
   joins on `<field>_token` has been re-keyed.

The rotation play deliberately requires the operator to load every
clear value through `pii_clear_audit.read_clear`, which writes one
audit-log row per access — so the rotation itself is auditable. The
salt is never rotated silently or as a side effect of a deploy.

## Clear-value reads — `pii:read_clear`

Direct ORM access to a `<field>_clear` column is a review-blocking
violation. The only sanctioned path is:

```python
from coherence_engine.server.fund.services.pii_clear_audit import (
    ClearReadPrincipal, PII_READ_CLEAR_SCOPE,
)

principal = ClearReadPrincipal(
    id=api_key.prefix,
    kind="api_key",
    scopes=("pii:read_clear", ...),
)
clear_email = founder.read_clear_email(
    db=db,
    principal=principal,
    route="/api/v1/founders/{id}/email",
    request_id=request.headers.get("x-request-id", ""),
    reason="ops_lookup",
)
```

`read_clear_email` does three things, in order:

1. Verifies that the principal carries the `pii:read_clear` scope.
   Failure raises `ClearReadDenied` (a `PermissionError` subclass);
   routers map this to HTTP 403.
2. Decrypts the per-row AES-GCM ciphertext under the row's key id.
   A shredded key here means the row has been crypto-shredded — the
   helper raises `KeyShreddedError`, which routers map to HTTP 410.
3. Inserts a `PIIClearAuditLog` row recording the principal id,
   field kind, token (never the clear value), subject id, route,
   and request id. The flush is on the request's session so a
   rollback rolls back both the read side effect and the audit
   row — preserving the invariant that an audit row exists iff the
   caller actually observed the clear value.

### Scope-catalog integration

`pii:read_clear` is the new scope referenced here. Adding it to
`KNOWN_SCOPES` in `server/fund/services/api_key_service.py` is the
final integration step before the `pii:read_clear` API can be issued
to a key by the admin UI; until then test principals can be
constructed directly through `ClearReadPrincipal(...)`.

## Audit log — `pii_clear_audit_log`

| column          | shape                                       | notes                                |
| --------------- | ------------------------------------------- | ------------------------------------ |
| `id`            | `String(40)` — `piiaud_<24hex>`             |                                      |
| `principal_id`  | API key prefix / service account id         | indexed                              |
| `principal_kind`| `api_key` \| `service_account` \| `user`    |                                      |
| `field_kind`    | one of `email`, `name`, `phone`, `address`  | indexed                              |
| `token`         | the value's tokenized form                  | indexed; **never the clear value**   |
| `subject_table` | source table name                           |                                      |
| `subject_id`    | source row id                               | indexed                              |
| `route`         | FastAPI path template the read came from    |                                      |
| `request_id`    | gateway access-log correlation id           | indexed                              |
| `reason`        | short tag (`ops_lookup`, `csv_export`, ...) |                                      |
| `note`          | free-form, must NOT contain clear PII       |                                      |
| `created_at`    | UTC timestamp                               | indexed                              |

### Tampering protection (RLS)

`PII_AUDIT_RLS_POLICIES` in `server/fund/security/rls.py` declares:

* `INSERT`: `service_role` only (the application server is the only
  writer).
* `SELECT`: `service_role` and `admin` (server reads and operator
  dashboard reads).
* No `UPDATE` or `DELETE` policy is declared for any role. Combined
  with default-deny RLS this is the on-disk guarantee that no role
  — including `service_role` — may modify or remove an audit row
  through the policy surface.

A defence-in-depth trigger
(`pii_clear_audit_log_no_update`) installed by the same migration
raises an exception on any `UPDATE` or `DELETE` statement, including
attempts that bypass RLS via a superuser connection. Failure mode:
the offending statement aborts and the surrounding transaction
rolls back — there is no path that mutates an audit row and commits.

## Prohibitions

* Do **not** log clear PII values anywhere — not in app logs, not in
  audit log fields, not in error messages. The `token` form is safe
  to log.
* Do **not** add an `UPDATE` or `DELETE` policy to `pii_clear_audit_log`
  for any role. The "INSERT-only" property is the entire point of the
  table.
* Do **not** substitute a non-cryptographic hash (md5, sha1, fnv, ...)
  for HMAC-SHA-256 in `tokenize`. Cryptographic strength is what makes
  the token non-reversible without the salt.
* Do **not** rotate `PII_TENANT_SALT` in-place via `kubectl set env`
  or similar — follow the documented salt rotation play above so
  every existing token gets recomputed atomically.
