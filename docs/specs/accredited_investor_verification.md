# Accredited Investor Verification (prompt 26)

## Legal disclaimer

**This software does not provide legal advice.** It implements an
operational adapter that records third-party attestations of an
investor's accredited status. Whether any particular Rule 501 path
(or any particular provider's attestation) is permissible for a given
fund structure, jurisdiction, or offering is a question for
**securities counsel**. Operators are responsible for confirming
which paths their offering may rely on.

The software does **not** "guarantee" accreditation. The verification
provider attests; the operator is responsible for the consequences of
relying on that attestation.

## Scope

This subsystem gates **LP intake** into the fund. It is independent
of the founder-application scoring pipeline (prompts 01-20) — a
founder's application is scored identically regardless of any
investor's verification state. Investors are the LP-side identity
that carries the verification record; founders remain unaffected.

## Rule 501 paths (methods)

The verification service tracks four canonical methods, drawn from
SEC Regulation D Rule 501(a):

| Method                          | Source                                         |
| ------------------------------- | ---------------------------------------------- |
| `income`                        | $200,000 individual / $300,000 joint income    |
| `net_worth`                     | $1,000,000 net worth excluding primary residence |
| `professional_certification`    | Series 7, Series 65, Series 82                 |
| `self_certified`                | Operator-attested only (lower trust)           |

The service does not enforce method semantics — it records what the
provider attests and surfaces it for the operator. A self-certified
record is structurally valid but carries lower trust than a
provider-attested method, and the operator UI should display this
distinction.

## Providers (backends)

Three pluggable backends ship in-tree:

* **Persona** (`PersonaBackend`) — Persona's identity-verification
  API. Income / net-worth / professional-certification flows are
  configured via the operator's Persona template.
* **Onfido** (`OnfidoBackend`) — Onfido's identity-verification API.
  Same Rule 501 path support as Persona.
* **Manual** (`ManualBackend`) — operator-attested. The "backend"
  records whatever evidence URI the operator uploads and lets a
  human flip status. There is no webhook for the manual provider;
  the verification service explicitly rejects any request that
  claims to be a manual-provider webhook.

Provider attestation matrix:

| Method                       | Persona | Onfido | Manual |
| ---------------------------- | ------- | ------ | ------ |
| `income`                     | ✓       | ✓      | ✓      |
| `net_worth`                  | ✓       | ✓      | ✓      |
| `professional_certification` | ✓       | ✓      | ✓      |
| `self_certified`             | (n/a)   | (n/a)  | ✓      |

The operator chooses the provider per investor at initiation time;
the chosen method depends on what evidence the investor provides
through the provider's flow.

## Lifecycle and statuses

```
   POST /investors/{id}/verification:initiate
                  │
                  ▼
              [pending]  ◄── webhook (signed)
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
   [verified]          [rejected]
        │
        │ + 90 days (lazy)
        ▼
   [expired]
```

Statuses:

* `pending`   — initiation succeeded; awaiting provider webhook.
* `verified`  — provider attests the investor is accredited under
  the recorded `method`.
* `rejected`  — provider attests the investor is not accredited.
* `expired`   — was `verified`, but `expires_at` has passed.

## Expiry

Verified records expire **90 days** from `completed_at`, per the SEC
re-verification convention. Expiry is evaluated lazily: the row is
not mutated when the clock crosses the boundary; reads recompute the
effective status based on `expires_at` and surface `expired` to the
caller. To restore `verified`, the operator initiates a fresh
verification — re-verification is a new row, not a mutation of
history.

## Webhook signature verification

Both Persona and Onfido sign webhook deliveries with HMAC-SHA-256
over the *raw* request body. The signature header carries the hex
digest of `HMAC-SHA-256(secret, f"{timestamp}.{raw_body}")`; the
timestamp is checked for `±5 min` skew so a captured webhook can
not be replayed indefinitely.

Verification uses `hmac.compare_digest` for constant-time comparison.

* `PERSONA_WEBHOOK_SECRET` — required for `/api/v1/webhooks/persona`.
* `ONFIDO_WEBHOOK_TOKEN`   — required for `/api/v1/webhooks/onfido`.

An invalid or stale signature returns `401 UNAUTHORIZED` and **does
not** mutate any row. **Signature verification is never bypassed**,
even in dry-run / test environments — the test suite exercises the
verifier with a known secret rather than with a "skip" flag.

## Replay protection

A webhook delivering the same `(status, method)` for an already-
terminal record (`verified` / `rejected`) is a no-op: the response
is `200`, no field is rewritten, and no `investor_verification_updated`
event is re-emitted. This protects against:

* Provider retry storms after a transient ingest failure on our side.
* Same-payload replays by an attacker who captured a delivery
  (signature timestamp skew already bounds replay to ±5 min, but
  this is a second line of defense).

## Storage discipline

The `fund_verification_records` table holds:

* SHA-256 `evidence_hash` of the uploaded evidence payload.
* `evidence_uri` pointing at object storage (e.g. `s3://...` or
  `supabase-storage://...`).
* `provider` and `provider_reference` for correlation.

It does **not** hold the raw evidence bytes — uploaded W-2s,
brokerage statements, attorney letters never enter the database.
This is enforced at the service layer (`apply_webhook` only writes
the URI + hash) and documented as a prompt-26 prohibition.

## Event emission

A successful state transition (`pending → verified` or
`pending → rejected`) emits an `investor_verification_updated` event
to the outbox. Payload shape:

```json
{
  "investor_id": "inv_...",
  "record_id": "vrec_...",
  "provider": "persona|onfido|manual",
  "status": "verified|rejected",
  "method": "income|net_worth|professional_certification|self_certified",
  "expires_at": "2026-07-24T...Z"
}
```

The event is appended to `fund_event_outbox`; downstream consumers
(LP onboarding, capital-call workflow) subscribe via the existing
outbox dispatcher.

## API surface

| Method | Path                                                 | Auth                | Purpose                                        |
| ------ | ---------------------------------------------------- | ------------------- | ---------------------------------------------- |
| POST   | `/api/v1/investors/{id}/verification:initiate`        | Investor JWT or service-role key | Start a verification; pick provider from body |
| GET    | `/api/v1/investors/{id}/verification`                 | Investor JWT or service-role key | Fetch latest record + effective status         |
| POST   | `/api/v1/webhooks/persona`                            | HMAC signature      | Provider-driven status update                  |
| POST   | `/api/v1/webhooks/onfido`                             | HMAC signature      | Provider-driven status update                  |

The investor JWT path uses the same Supabase `sub` claim as the
founder JWT (prompt 25); `current_investor` is the LP-side parallel
of `current_founder`. A Supabase user can have both a `Founder` and
an `Investor` row keyed by the same `sub`.

## Operator responsibilities

* Confirm with securities counsel which Rule 501 path(s) the offering
  may rely on.
* Configure the chosen provider's template for the relevant method.
* Treat `self_certified` as lower-trust and surface that distinction
  in any LP-facing UI.
* Re-initiate verification after 90 days for any LP whose record
  has expired.
* Audit `fund_event_outbox` for `investor_verification_updated`
  events to drive downstream LP-onboarding workflows.
