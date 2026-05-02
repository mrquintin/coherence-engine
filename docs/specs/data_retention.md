# Data retention, GDPR/CCPA right-to-delete, crypto-shredding (prompt 57)

This document is the operator-facing specification for how the fund
backend retains, redacts, and erases user data. The system-of-record
for retention windows is `data/governed/retention_policy.yaml`; this
spec explains what the YAML *means* and how the retention worker +
`POST /api/v1/privacy/erasure` endpoint enforce it.

## Threat model and goals

Two distinct legal pressures:

1. **GDPR Art. 17 / CCPA right-to-delete.** A founder (or, in the LP
   path, an investor) can demand deletion of their personal data. We
   must comply within ~30 days while not destroying the immutable
   audit trail regulators and LPs rely on.
2. **Audit and recordkeeping.** Decision artifacts (the
   `decision_artifact.v1` immutable bundles produced by the scoring
   pipeline) and the API audit log MUST survive any subject erasure
   request. They are the system of record for "did the fund make a
   compliant decision on this application?" and a regulator inquiry
   five years later must still find them.

The technique that lets both goals coexist is **crypto-shredding**:
each high-PII row holds a per-row AES-256-GCM key. When the retention
horizon hits (or a verified erasure request is processed), we destroy
the key, leaving the encrypted ciphertext untouched but mathematically
unrecoverable. The row continues to exist (so foreign keys stay
intact) but its plaintext is gone.

## Data classes and policy

`data/governed/retention_policy.yaml` is the canonical declaration.
Every class has:

* `name` — the class identifier the retention worker dispatches on.
* `retention_days` — number of days from row creation; `indefinite`
  for audit-hold classes.
* `on_expiry` — one of `tombstone_and_shred` (the default workflow)
  or `keep` (audit hold; never auto-deleted).

| class                | retention                  | on_expiry              | rationale                                                                                       |
| -------------------- | -------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------- |
| `transcript`         | 90 days                    | `tombstone_and_shred`  | GDPR data minimisation; the scoring pipeline only needs the transcript during the active cycle. |
| `interview_recording`| 90 days                    | `tombstone_and_shred`  | Mirrors `transcript`; recording is the upstream artifact.                                        |
| `decision_artifact`  | indefinite                 | `keep`                 | Immutable audit trail; required for regulator + LP inquiries. **Never erased by subject request.** |
| `audit_log`          | indefinite                 | `keep`                 | API key audit + breach forensics. **Never erased.**                                             |
| `kyc_evidence`       | 1825 days (5 yrs)          | `tombstone_and_shred`  | BSA recordkeeping floor for fund operators (31 CFR 1010.430). Verify with counsel.              |
| `founder_pii`        | 2555 days (~7 yrs)         | `tombstone_and_shred`  | Long-tail tax / dispute window; older rows are shredded in place.                               |

> **Counsel review required.** The 1825-day BSA window and 2555-day
> founder-PII window are starting points based on common fund-operator
> norms. Each operator must validate against their jurisdiction and
> any applicable LP-side contractual requirements.

### What is retained indefinitely (and why)

The two `keep` classes are explicit and small:

* `decision_artifact` — every `Decision` row plus the canonical
  `decision_artifact.v1` JSON it points at. Subject erasure of these
  rows is **refused** with `ERASURE_REFUSED_AUDIT_HOLD`.
* `audit_log` — `fund_api_key_audit_events` and analogues. The
  privacy event publisher itself emits new audit log rows when an
  erasure runs, so the trail of *what was erased* is preserved even
  when the *contents* are not.

A subject erasure that removes a founder's transcripts, recordings,
KYC evidence, and PII columns will still leave the founder's
`Decision` row intact. This is intentional: the `founder_id` foreign
key continues to exist as an opaque pseudonym, and the
audit-trail-side queries ("what verdict did we issue against
application X?") still resolve.

## Crypto-shredding mechanics

### Per-row encryption (`server/fund/services/per_row_encryption.py`)

* Cipher: **AES-256-GCM**.
* 96-bit nonce per encryption from `os.urandom`. (Re-using a
  (key, nonce) pair under AES-GCM is catastrophic; the helper never
  accepts a caller-supplied nonce.)
* Associated data (AAD): the row's logical id. A ciphertext from row
  A pasted into row B fails authentication.
* Encoded ciphertext format: `b64(0x01 || nonce(12) || ct||tag)`.
* Key material is fetched from a pluggable
  `EncryptionKeyStore`. The default reads
  `fund_encryption_keys.key_material_b64`; production deployments
  inject a KMS-backed store via `set_encryption_key_store(...)` and
  the key column becomes a KMS handle rather than raw bytes.

### Shredding (`server/fund/services/crypto_shred.py`)

`shred_key(db, key_id)` zeroes `key_material_b64` and stamps
`shredded_at`. The row is preserved so audit logs that reference the
key_id stay interpretable. Idempotent: a second shred is a no-op
returning `False`.

After shredding, any
`per_row_encryption.decrypt(ct, db=..., row_id=..., key_id=...)` call
raises `KeyShreddedError`. Read endpoints map this to **HTTP 410 Gone**
with the row's `redaction_reason` so the requestor learns *why* the
data is gone (e.g. `retention:transcript` vs `erasure:era_...`).

## The daily retention worker

`server/fund/services/retention.py::apply_retention(db)` is the entry
point. It walks every class in the policy YAML and, for each
`tombstone_and_shred` class:

1. Selects rows whose age column (created_at / started_at) is older
   than `retention_days`.
2. Calls `object_storage.delete(uri)` for each URI on the row. This
   is the soft-delete from prompt 29 — the live blob is copied to a
   `tombstone/` prefix and the original key removed. Hard purge is a
   separate admin verb.
3. Calls `crypto_shred.shred_key(db, row.<class>_key_id)` to destroy
   the per-row key.
4. Sets `redacted=True`, `redacted_at=<now>`, and
   `redaction_reason=retention:<class_name>`.

The worker is idempotent: redacted rows are filtered out of the
sweep query, so a second run produces zero side-effects.

## The erasure endpoint

`POST /api/v1/privacy/erasure` is a two-phase workflow.

### Phase 1: support staff issue a token

```bash
POST /api/v1/privacy/erasure/issue   (admin / support roles only)
{
  "subject_id": "fnd_...",
  "subject_type": "founder"
}

200 OK
{
  "data": {
    "erasure_request_id": "era_...",
    "verification_token": "<one-shot URL-safe token>",
    "subject_id": "fnd_...",
    "subject_type": "founder",
    "expires_in_days": 30
  }
}
```

The plaintext token is returned **once** in this response and never
persisted anywhere else. Only its SHA-256 hash is written to
`fund_erasure_requests.verification_token_hash`. Support hands the
token to the verified subject through a vetted out-of-band channel
(phone callback, in-person, video KYC). Identity verification happens
before the token is issued — the server does not (and cannot)
re-verify the subject's identity from the API call alone.

### Phase 2: subject schedules the erasure

```bash
POST /api/v1/privacy/erasure
{
  "subject_id": "fnd_...",
  "verification_token": "...",
  "classes": ["transcript", "interview_recording"],   // optional
  "immediate": false                                   // admin only
}
```

The handler:

1. Hashes the token server-side and looks up the matching
   `ErasureRequest` row. A miss (token doesn't exist or `subject_id`
   mismatch) returns `401 UNAUTHORIZED`.
2. Splits the requested classes against the policy. Any class flagged
   `on_expiry: keep` (decision_artifact, audit_log) refuses the
   entire request with `ERASURE_REFUSED_AUDIT_HOLD` and emits an
   `erasure_refused` event. The handler does **not** silently drop
   the audit-hold class and proceed with the rest — refusing is
   loud-and-explicit so the subject can escalate to support.
3. For non-audit-hold requests, transitions to `scheduled` with
   `scheduled_for = now + 30 days` and emits `erasure_scheduled`.
4. Returns `status="scheduled"`. The handler **never** returns
   `"completed"` — the daily worker flips that flag once it has
   actually executed the deletion (`execute_erasure(db, request_id)`).

`immediate=true` collapses the 30-day buffer to zero but is gated to
the `admin` role.

### Idempotency

A replayed request (same token) returns the existing record with
`idempotent: true` and no side effects. The 30-day buffer is set on
the first call only.

## Test plan

* `tests/test_retention.py` covers policy YAML parsing, the daily
  sweep against an aged transcript, the audit-hold no-op for
  `decision_artifact`, idempotency, and AES-GCM AAD binding.
* `tests/test_erasure_endpoint.py` covers token issuance, the
  `UNAUTHORIZED` path on token mismatch, the
  `ERASURE_REFUSED_AUDIT_HOLD` path, idempotent replays, and the
  "no completion before worker runs" contract.

## Operator runbook

1. **Wiring the privacy router into the FastAPI app.** Mount
   `coherence_engine.server.fund.routers.privacy.router` under
   `/api/v1` in `server/fund/app.py`. (This wiring lives outside
   prompt 57's scope but is a one-line follow-up.)
2. **Scheduling the daily worker.** The retention sweep runs once
   per day; wire it into `services/scheduled_jobs.py` or your cron
   equivalent.
3. **Token reset.** A leaked verification token is rotated by
   creating a new `ErasureRequest` row (new token) and invalidating
   the leaked one (set `status='refused'`,
   `refusal_reason='token_revoked'`).
4. **KMS swap.** When a KMS is wired, call
   `per_row_encryption.set_encryption_key_store(KmsBackedStore())`
   at app boot. Existing rows continue to decrypt under the legacy
   store via a fallback adapter; new rows write through the KMS.
   (The legacy store is kept around so rotation can run without a
   full-table re-encryption.)

## Prohibitions (re-asserted from prompt 57)

* **Do not erase audit-trail data.** Decision artifacts and audit
  logs are `on_expiry: keep`. The endpoint refuses requests that
  target them with a clear, machine-readable code.
* **Do not confirm erasure to the requestor before the deletion job
  has actually completed.** The handler returns `"scheduled"`; only
  the worker writes `"completed"`.
* **Do not trust client-supplied `verification_token` without
  server-side validation.** The handler hashes the token and looks
  up the row server-side; the request body is never trusted as the
  authority on subject identity.
