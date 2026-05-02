# Cap-Table Integration (prompt 68)

## Purpose

After an investment workflow has reached its terminal "operator caused
it" state, sync the resulting issuance to the configured cap-table
provider for record-keeping. Carta is the primary integration; Pulley
is the alternate.

The system **records** issuances. It does **not** unilaterally issue
securities. The local `CapTableIssuance` row is the source of truth;
the provider sync is downstream record-keeping that can be reconciled
but never drives the local state.

## Preconditions (load-bearing)

A `CapTableIssuance` MUST NOT be created against an application unless
*both* of the following hold:

1. A `SignatureRequest` for that application has reached
   `status = "signed"` (prompt 52).
2. An `InvestmentInstruction` for that application has reached
   `status = "sent"` (prompt 51).

The check lives in
`server.fund.services.cap_table.preconditions_satisfied(...)` and is
invoked by `CapTableService.record_issuance` (default
`verify_preconditions=True`). The service raises `PreconditionsNotMet`
when either gate is open.

## Lifecycle

```
pending --(provider sync)--> recorded --(reconcile)--> reconciled
                                      \
                                       +--> failed (terminal; retry creates a new key)
```

* `pending` -- local row created, backend has not yet acknowledged.
* `recorded` -- backend acknowledged, `provider_issuance_id` stored,
  `recorded_at` set.
* `reconciled` -- `CapTableService.reconcile(...)` re-read the
  provider record and confirmed every numeric field matches local.
* `failed` -- backend dispatch raised `CapTableBackendError`; row is
  terminal. A retry needs a new idempotency key.

## Idempotency

`compute_idempotency_key(application_id, instrument_type, salt)` is a
SHA-256 of the inputs. The `ApplicationService.maybe_sync_cap_table`
hook seeds `salt` from `InvestmentInstruction.id` so two distinct
funded instructions for the same application produce two distinct
issuance rows, while a webhook replay collapses onto one.

The provider's returned `provider_issuance_id` is informational only.
Local idempotency is keyed off `idempotency_key`; the provider's id
is recorded for audit but **never trusted as authoritative** (prompt
68 prohibition).

## Reconciliation

`CapTableService.reconcile(backend=...)` reads every local row in
`status = "recorded"` for the given backend and compares each row to
the provider's `fetch_issuance(provider_issuance_id=...)` response.

* Matching rows transition `recorded -> reconciled`.
* Mismatching rows produce a `ReconciliationFinding` per divergent
  field; the local row is **never** silently rewritten to match the
  provider. The operator decides how to resolve the divergence
  (typically by correcting the provider record out of band).
* Missing remote (provider returns 404 / not found) is collected in
  `report.missing_remote` for operator triage.

## Wiring

The trigger lives in `ApplicationService.maybe_sync_cap_table`. It is
designed to be called from both:

* the e-signature webhook handler, after a `SignatureRequest`
  transitions to `signed`; and
* the capital webhook handler, after an `InvestmentInstruction`
  transitions to `sent`.

Whichever side fires second satisfies the precondition check and
performs the sync; the other side returns `None` because the gate is
still half-open. Repeated calls are idempotent.

## Backends

Selected by the `CAP_TABLE_PROVIDER` env var (`carta` | `pulley`).
Each backend reads its own API token from the environment:

| Backend  | Env var(s)                            |
| -------- | ------------------------------------- |
| Carta    | `CARTA_API_TOKEN`, `CARTA_API_BASE`   |
| Pulley   | `PULLEY_API_TOKEN`, `PULLEY_API_BASE` |

In default-CI configuration the backends do NOT make real network
calls. The live HTTP code paths are gated on a real API token and are
exercised only in staging / prod.

## Allowed instrument types

`safe_post_money | safe_pre_money | priced_round_preferred`. Anything
else raises `CapTableError`; convertible notes etc. are intentionally
out of scope until the operator-managed instrument vocabulary is
extended.

## Operator obligation

A cap-table issuance is a securities action. Production use requires
that the SAFE / term-sheet template (prompt 52) has been reviewed by
securities counsel and that board-consent evidence
(`board_consent_uri`) has been recorded. This software does not
provide legal advice.
