# Capital Deployment (prompt 51)

This module wires the fund's capital-deployment surface: the path
between an approved application and an actual money movement to the
founder's bank or Stripe-Connect-attached account.

The defining property of this surface is the **non-autonomy
invariant**: the software *prepares* a transfer instruction and emits
the events that put it on a treasurer's queue, but only an authorized
human (operator role: `treasurer`) can approve it, and only the same
or another `treasurer` can execute it. There is no code path that
moves funds without a corresponding `TreasurerApproval` row in the
database.

## State machine

```
prepared --(treasurer approve)--> approved --(treasurer execute)--> sent
   |                                  |                              |
   |                                  |                              v
   |                                  |                            failed
   |                                  v
   +------------------ cancelled <----+
```

* **`prepared`**: the partner-side `prepare` call has registered an
  `InvestmentInstruction` and obtained an upstream
  `provider_intent_ref`. No money has moved.
* **`approved`**: at least one `TreasurerApproval` row exists; the
  instruction is eligible for execution. Amounts at or above
  `CAPITAL_DUAL_APPROVAL_THRESHOLD_USD` (default $250,000) require two
  distinct treasurer approvals before `execute` succeeds.
* **`sent`**: the backend has acknowledged the transfer. The provider
  webhook later confirms terminal success (`transfer.paid` /
  `payment_intent.succeeded`) or escalates to `failed`.
* **`failed`**: backend rejected the transfer or webhook reported a
  terminal failure. The error is captured in `error_code`.
* **`cancelled`**: an authorized operator cancelled before execute.

## Endpoints

| Verb | Path | Role | Effect |
|---|---|---|---|
| POST | `/api/v1/capital/instructions:prepare` | partner / treasurer / admin | Register + return a new `prepared` instruction. Idempotent on `Idempotency-Key`. |
| POST | `/api/v1/capital/instructions/{id}:approve` | treasurer / admin | Record one treasurer approval; transitions to `approved` on the first call. |
| POST | `/api/v1/capital/instructions/{id}:execute` | treasurer / admin | Dispatch the upstream transfer. 403 if not yet approved or if dual approval is required and only one approval exists. |
| POST | `/api/v1/webhooks/stripe` | (HMAC-signed) | Reconcile terminal status from Stripe. |

## Backends

Two pluggable backends implement the `CapitalBackend` protocol in
`server/fund/services/capital_backends.py`:

* `StripeConnectBackend` — Stripe Connect transfers for non-US founder
  payouts and platform-style flows. Reads `STRIPE_SECRET_KEY` and
  `STRIPE_CONNECT_ACCOUNT_ID` from the environment. The webhook secret
  (`STRIPE_WEBHOOK_SECRET`) is required for the `/webhooks/stripe`
  route.
* `BankTransferBackend` — ACH/wire via Mercury (or another bank API
  with the same counterparty contract). Reads `MERCURY_API_TOKEN`.
  `prepare` calls the provider's counterparty-verification endpoint;
  raw account / routing numbers never enter our system, only the
  provider-issued counterparty token (`cp_…`).

In default CI configuration both backends use deterministic synthetic
in-tree code paths so unit tests can exercise the prepare / execute
state machine without HTTP. Live HTTP code is gated on real API keys
and is only exercised in staging / prod.

## Events

Two outbox events accompany the lifecycle:

* `investment_funding_prepared.v1` — emitted on a successful
  `prepare`. The downstream consumer notifies the treasurer queue
  (Slack channel, email, partner dashboard).
* `investment_funded.v1` — emitted on a successful `execute`
  (post-backend acknowledgement). A subsequent webhook may upgrade
  the instruction's `error_code` on terminal failure but does not
  re-emit the event.

Schemas live alongside the others in
`server/fund/schemas/events/`.

## Storage discipline

* `target_account_ref` is the provider's counterparty token. The
  database NEVER holds raw bank account or routing numbers.
* `TreasurerApproval` rows are append-only; the unique index on
  `(instruction_id, treasurer_id)` rejects duplicate sign-offs from
  the same operator (so dual approval cannot be faked by the same
  human pressing the button twice).
* Idempotency: `prepare` collapses on `Idempotency-Key`. The same
  client retrying the same logical request gets the same
  instruction id back; no duplicate event is emitted.

## Prohibitions (from prompt 51)

The following are enforced by the service layer and / or the
database, NOT by convention alone:

1. `execute` MUST NOT run without a `TreasurerApproval` row. The
   service raises `InstructionStateError("execute_requires_approval")`
   and the router returns 403.
2. `execute` MUST NOT bypass dual approval at amounts at or above
   `DUAL_APPROVAL_THRESHOLD_USD`. The service raises
   `InstructionStateError("execute_requires_dual_approval")` and the
   router returns 403.
3. Raw bank account / routing numbers MUST NOT be stored. Only the
   provider counterparty token enters the database, and the bank API
   verifies the counterparty out-of-band before a token is minted.
4. The default test suite MUST NOT make live calls to Stripe or
   Mercury. The `backend_for_method` factory is replaced by
   `set_backend_factory_for_tests` in
   `tests/test_capital_deployment.py`.
5. The system MUST NOT execute transfers autonomously. There is no
   scheduler, no auto-approval, and no code path that calls
   `CapitalDeployment.execute` from a background worker. Every
   execute is the result of an authenticated treasurer hitting the
   `…:execute` route.

## Operational runbook

* If a transfer is returned by the bank, the provider webhook will
  flip the row to `failed`. Operators correct the counterparty token
  out-of-band, then start a new `prepare` (do NOT re-use the old
  instruction id — its idempotency key has been spent).
* If a treasurer approves the wrong instruction, they can cancel it
  via the (operator-only) cancel path on the service. Cancellation
  is not exposed as a route by default; cancel must be performed
  through a deliberate operator action so that an accidental DELETE
  cannot wipe the audit trail.
