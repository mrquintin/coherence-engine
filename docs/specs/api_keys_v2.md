# API Keys v2 — service-account scoped, rotatable, hashed

**Status:** in force as of prompt 28 (Wave 8). Supersedes the legacy
single-`role` API-key model from prompt 9.

## Why we changed the model

The legacy table stored credentials as raw **SHA-256** digests, gated
authorization on a single coarse `role` enum, and had no per-key rate
limit, no rotation tooling, and no formal expiry policy. SHA-256 is a
fast, unsalted message digest — fundamentally the wrong primitive for
credential storage. A 32-byte token whose hash leaks via backup, replica
read, or audit log can be brute-forced offline at GPU rates because
each guess costs a single hash invocation.

The v2 model fixes all of the above:

* **Argon2id** for hashing (memory-hard, salted, OWASP-recommended).
* Explicit **scopes** rather than a coarse role enum.
* Per-key **rate limit**.
* Hard 1-year **expiry** by default; rotation is a first-class operation.
* **Service accounts** group keys by owner so rotation responsibility is
  unambiguous.

## Scope catalog

A key carries a list of strings drawn from a fixed enum (defined in
`coherence_engine.server.fund.services.api_key_service.KNOWN_SCOPES`):

| Scope                  | Grants                                                                |
|------------------------|-----------------------------------------------------------------------|
| `applications:read`    | Read founder applications, intake state, scoring artifacts.           |
| `applications:write`   | Create / mutate founder applications.                                 |
| `decisions:read`       | Read fund decisions, escalation packets.                              |
| `admin:read`           | Read admin-surface telemetry, key metadata, audit trail.              |
| `admin:write`          | Mutate admin surfaces (create / revoke / rotate keys, accounts).      |
| `worker:claim`         | Claim a queued scoring job (lease).                                   |
| `worker:complete`      | Complete or fail a scoring job.                                       |

Subset semantics: a route declares the set of scopes it requires; a
key matches if it carries every required scope. Extra scopes on the key
are ignored.

```python
from coherence_engine.server.fund.security.api_key_auth import require_scopes

@router.post("/scoring/jobs/claim")
def claim(_=Depends(require_scopes("worker:claim"))):
    ...
```

## Token format and the "plaintext shown once" invariant

Tokens are formatted:

```
ce_<prefix>_<secret>
```

where `prefix` is an 8-character lowercase alphanumeric public
discriminator and `secret` is 32 bytes of base64url-encoded entropy.

Authentication is two-step:

1. **Lookup by prefix.** The 8 prefix characters identify a row
   directly via an indexed equality match. This is O(1) and reveals
   nothing more than the prefix already revealed.
2. **Argon2id verify.** The full presented token is verified against
   the stored hash. Verification is constant-time — even an unknown
   prefix triggers a dummy hash so timing does not reveal whether a
   prefix exists.

The plaintext token is returned **exactly once** by the create / rotate
operations. The server never persists it — only the Argon2id hash is
stored. There is no recovery mechanism: if the token is lost, rotate
the key.

The CLI / admin API both print:

```
# WARNING: plaintext token shown once and never again. Store it now.
```

This is not advisory. There is no second chance.

## Lifecycle

### Create

```
coherence-engine api-keys create \
    --account scoring-worker \
    --scope worker:claim \
    --scope worker:complete \
    --expires 2027-04-25
```

If `--account` does not exist yet, the CLI creates the service account
on the fly using `--description` and `--owner-email`.

The same surface is exposed over the admin API at
`POST /admin/api-keys/v2`.

### List

```
coherence-engine api-keys list --account scoring-worker
```

Lists prefix, scopes, expiry, revocation, and last-used per key.
**Plaintext tokens are never printed by `list`** — they were already
disclosed at create / rotate time.

### Revoke

```
coherence-engine api-keys revoke --prefix qu73h8k5
```

Sets `revoked_at = now()`. Revoked keys fail authentication with
HTTP 401 + error code `UNAUTHORIZED_REVOKED` (distinct from a generic
`UNAUTHORIZED` so dashboards can alert on unexpected use of revoked
credentials).

### Rotate

```
coherence-engine api-keys rotate --prefix qu73h8k5 --grace-seconds 300
```

Rotation creates a fresh key with the same scopes, rate limit, and
service account, and shrinks the old key's `expires_at` to
`now + grace_seconds`. With the default `--grace-seconds 0` the old key
is revoked immediately; non-zero values let in-flight callers swap
tokens without an authorization gap.

## Rotation playbook

A key is **due for rotation** when any of the following is true:

* `expires_at` is within 14 days of now.
* The key was last used by a service that has been redeployed under a
  new owner.
* A secret-management incident has plausibly exposed the secret store.
* The owner of the service account has changed roles or left the org.

Standard sequence:

1. Decide the grace window. Workers polling on a long lease should use
   `--grace-seconds 600`. Synchronous front-doors with bounded request
   timeouts can use `--grace-seconds 60`. Anything that pushes the new
   token to its consumer atomically can use `--grace-seconds 0`.
2. Run `api-keys rotate --prefix <old> --grace-seconds <N>`.
3. Capture the **plaintext** token from stdout exactly once and push
   it through your secret-management surface (e.g.
   `coherence-engine secrets put`).
4. Wait for the consumer to pick up the new secret. Confirm via the
   service's own log line / health probe that it has rolled.
5. (Optional) Run `api-keys revoke --prefix <old>` once you've confirmed
   the cut-over, even before the grace window elapses.

The audit trail (`fund_api_key_audit_events`) records both the create
and the revoke / rotate as discrete rows tied to the new and old key
ids; that ledger is the source of truth for "when did we rotate?"
post-mortems.

## Per-key rate limiting

Each key carries a `rate_limit_per_minute` (default 60). The
`require_scopes(...)` dep maintains a per-process token bucket keyed by
prefix; a request that drains the bucket returns HTTP 429 with code
`RATE_LIMITED`.

Cluster-wide rate limiting is a gateway concern and is **out of scope**
for this prompt. Per-process limits are sufficient for development and
for single-replica deployments; multi-replica deployments should
configure the upstream API gateway (nginx, Envoy, AWS API Gateway,
etc.) to enforce the same per-key budget across replicas.

## Migration from legacy

The migration `20260425_000004_service_accounts_and_api_keys_v2`
**drops the legacy `fund_api_keys` table** and recreates it under the
v2 schema. Legacy keys cannot be re-issued onto the new hash algorithm
because the plaintext is unrecoverable from a SHA-256 digest.

**Operational impact:** all existing API keys are invalidated by the
migration. Operators must re-issue any in-use keys before deploying
prompt 28 to production:

```
coherence-engine api-keys create --account <name> --scope <s> [...]
```

Audit-event rows that referenced the dropped keys are preserved with
`api_key_id = NULL` (orphaned) so the historical action / actor /
timestamp / IP / path remain queryable.

## Prohibitions (enforced by review)

* **No SHA-256 / SHA-1 / MD5** for credential hashing. Argon2id only.
* **Never persist plaintext tokens** anywhere — not in the database,
  not in logs, not in audit details, not in error responses.
* **Never log a token** even at DEBUG level. Log the 8-char prefix; it
  is safe to disclose.
* **Never bypass `require_scopes`** with a "trust me, I'm authenticated"
  shortcut. Every protected route must declare the exact scopes it
  needs.
* **Never reuse the legacy validation path** (`hash_token` /
  `get_by_hash`). Those entry points are kept only as compatibility
  wrappers around the v2 path during the transition; new code calls
  `ApiKeyService.verify_key` or `require_scopes(...)` directly.
