# Founder KYC/AML Screening (prompt 53)

## Why founder KYC is upstream of capital instructions

Capital deployment to a founder is a regulated act. Three distinct
risk surfaces have to be cleared before any wire / SAFE / cash
movement:

1. **Sanctions screening** — the founder is not on OFAC SDN, EU
   consolidated, UK HMT, or UN sanctions lists.
2. **PEP screening** — the founder is identified as a politically
   exposed person (or relative / close associate of one), and either
   cleared or escalated to the operator's enhanced-due-diligence
   workflow.
3. **ID + AML verification** — the founder's claimed legal identity
   is bound to a real-world document (passport, government ID), and
   the application's funding sources do not raise an AML hit.

This is **separate** from prompt 26's accredited-investor flow. That
flow gates LP/investor side capital intake under SEC Rule 501 — who
is putting capital *in*. KYC gates the founder side — who is
receiving capital *out*. Confusing the two would mean either
(a) accidentally letting a sanctioned founder receive capital
because the LP gate is green, or (b) refusing capital to an
unaccredited founder, which makes no sense (founders are not
investors).

The two flows therefore have:

* Separate tables: `fund_verification_records` (LP) vs
  `fund_kyc_results` (founder).
* Separate provider env vars: `PERSONA_API_KEY` vs
  `PERSONA_KYC_API_KEY`, `ONFIDO_API_TOKEN` vs `ONFIDO_KYC_API_TOKEN`,
  and analogous webhook-secret splits. A leaked LP secret cannot
  forge a founder webhook.
* Separate routers and webhook URLs:
  `/investors/{id}/verification:initiate` vs
  `/founders/{id}/kyc:initiate`,
  `/webhooks/persona` vs `/webhooks/founder_kyc/persona`.

## Lifecycle

```
pending --(provider webhook)--> passed
                       |--> failed
                       |--> expired   (lazy, evaluated read-side)
```

* **pending**: initiation persisted, awaiting provider webhook.
* **passed**: all enabled screening categories returned no hit.
  Valid for `KYC_TTL_DAYS = 365` from `completed_at`.
* **failed**: at least one enabled category produced a hit.
  *Routes to operator manual review — never to automatic permanent
  rejection of the founder.*
* **expired**: derived state. A `passed` row whose `expires_at` has
  elapsed reads as `expired`; the underlying row is not mutated.

## Decision-policy gate (`kyc_clear`)

`server/fund/services/decision_policy.py` exposes a hard gate keyed
on `application["kyc_passed"]`. The contract is:

| `kyc_passed` value | Behavior                                       |
|--------------------|------------------------------------------------|
| `True`             | Gate clears. Verdict can be `pass`.            |
| `False`            | Gate fails: `kyc_clear` / `KYC_REQUIRED`.      |
| `None` / absent    | Backward compatibility — gate not enforced.    |

When `KYC_REQUIRED` is among the failed gates, the verdict
downgrades to `manual_review` (not `fail`) per the prompt 53
prohibition: a single KYC failure must not auto-reject the founder
forever. Hard-fail codes (`COMPLIANCE_BLOCKED`, etc.) still take
precedence — those are independently disqualifying.

Production callers thread the gate through
`founder_kyc.is_kyc_clear(record)`, which evaluates the most recent
`KYCResult` for the founder and applies expiry semantics.

## Refresh cadence

* TTL: `KYC_TTL_DAYS = 365` days (annual). Computed from
  `completed_at` at the moment the provider returns `passed`.
* Notice window: `KYC_REFRESH_NOTICE_DAYS = 30` days before
  `expires_at`, the daily `scan_refresh_due` job emits one
  `founder_kyc.refresh_due` outbox event per nearing-expiry row.
  The event's `idempotency_key` is keyed on `result_id +
  expires_at`, so re-running the daily scan does not double-emit.
* **Re-screen on every funding event**: the application_service is
  expected to call `is_kyc_clear` immediately before issuing any
  new capital instruction. The decision-policy gate is the
  enforcement point — a pre-existing `passed` row whose
  `expires_at` has elapsed reads as `expired` and the next pass
  attempt downgrades.

## Storage discipline (prohibition)

* Raw KYC document content (passport scans, utility bills,
  sanctions-payload bytes) **never enters the database**.
* Only the SHA-256 hash of the evidence (`evidence_hash`) and the
  provider's opaque reference (`evidence_uri`, e.g. an
  object-storage URI or a Persona inquiry id) are persisted.
* Webhook bodies are HMAC-SHA-256 verified with `hmac.compare_digest`
  over the raw request bytes, with a 5-minute timestamp-skew check.
  Signature verification is **never bypassed**, even in dry-run mode.

## Handling a failed KYC

A `failed` status is *not* an automatic permanent ban. The operator
UI surfaces the `KYCResult` row (provider, failure_reason,
screening_categories that hit) on the founder's manual-review
queue. The operator workflow:

1. Inspect the provider's evidence at `evidence_uri` (or fetch
   directly from the provider portal using `provider_reference`).
2. Decide: false positive → manually re-screen (start a fresh
   attempt with a different provider or after corrected metadata);
   true positive → escalate to enhanced due diligence; clear
   sanctions hit → close the application as ineligible *for this
   round* with documented reason; never blacklist the founder
   identity by ID alone, since a sanctions list can clear over
   time.
3. The decision verdict remains `manual_review` until either a new
   `KYCResult.passed` is recorded (which clears the gate on the
   next decision-policy evaluation) or the operator explicitly
   closes the application via the override path
   (`docs/specs/decision_overrides.md`).

This guardrail — manual review, never auto-reject-forever —
matters because KYC providers do produce false positives,
especially on common names. Permanently blacklisting a founder on
the first hit would systematically deny capital to people whose
identity simply collides with a flagged record.

## Operator runbook (env vars)

| Variable                       | Required | Notes                                  |
|--------------------------------|----------|----------------------------------------|
| `PERSONA_KYC_API_KEY`          | yes      | Persona founder-KYC inquiry creation.  |
| `PERSONA_KYC_WEBHOOK_SECRET`   | yes      | HMAC secret for Persona deliveries.    |
| `PERSONA_KYC_TEMPLATE_ID`      | no       | Persona template id for KYC inquiries. |
| `ONFIDO_KYC_API_TOKEN`         | yes      | Onfido founder-KYC API token.          |
| `ONFIDO_KYC_WEBHOOK_TOKEN`     | yes      | Onfido webhook HMAC secret.            |

These intentionally do *not* reuse the LP-flow secrets. Operators
running both flows in production should provision two distinct
applications inside the provider's dashboard (one per flow).
