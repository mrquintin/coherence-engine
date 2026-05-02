# CRM bidirectional sync (prompt 55)

## Goal

Mirror `Founder` and `Application` records to a CRM (Affinity primary,
HubSpot alternate) and absorb partner-side edits flowing back via
webhooks. A daily reconciliation job repairs any deliveries the
webhook listener missed.

## Surfaces

| File | Responsibility |
| --- | --- |
| `server/fund/services/crm_backends.py` | `AffinityBackend` / `HubSpotBackend`. Signature verification, `parse_webhook`, deterministic synthetic `upsert_*` ids. |
| `server/fund/services/crm_sync.py`     | `enqueue_outbound_upsert`, `apply_inbound_update`, `reconcile_crm_deltas`. |
| `server/fund/routers/crm_webhooks.py`  | `POST /webhooks/crm/affinity`, `POST /webhooks/crm/hubspot`. 401 on bad signature, never mutate state without a verified signature. |
| `server/fund/services/scheduled_jobs.py` | Cron registry. `crm_daily_reconciliation` runs once per day per backend. |

## Outbound flow

`ApplicationService` calls `enqueue_outbound_upsert(db,
application_id=..., reason=...)` whenever `Application.status` or
`Decision.decision` changes. The function writes a
`crm_upsert_requested` event into `fund_event_outbox`. A downstream
worker picks it up and calls `CRMBackend.upsert_founder` /
`upsert_application`.

The decision to enqueue rather than call synchronously is intentional:
a CRM outage cannot stall application progression, and the outbox
already provides retries / dead-lettering.

## Inbound flow

```
HTTP POST -> verify_webhook -> parse_webhook -> apply_inbound_update
```

`apply_inbound_update(db, update: CRMUpdate)`:

1. Resolves the local `Application` -- prefers explicit
   `application_id`, falls back to the most-recent application for
   `founder_email`.
2. Reads the most-recent `crm_inbound_applied` event for
   `(provider, external_id)` as the prior mirror snapshot.
3. Carries forward any field the new payload omits (CRM null is **not**
   a clear; it is "no signal").
4. Compares to the snapshot. If unchanged, returns
   `{"applied": false, "reason": "already_current"}`.
5. Otherwise persists a new `crm_inbound_applied` event whose payload
   carries the merged tags / notes / deal-stage. The payload also
   carries `verdict_locked: true` as the explicit marker that this
   path does NOT mutate `Decision.decision`.

### Conflict policy

* Tags / notes / deal-stage labels: **last-writer-wins**. Whatever the
  CRM webhook delivered replaces the prior mirror.
* `Decision.decision` (the verdict): **never** modified by an inbound
  CRM event. Verdicts are produced exclusively by
  `decision_policy`; partner-side stage labels are recorded in the
  ledger only.
* CRM null / missing field: **no-op**, not a clear. We do not delete
  local state on the basis of a CRM null.

## Reconciliation

`reconcile_crm_deltas(db, backend, *, now=None,
window=timedelta(hours=24))` runs once per day:

1. Calls `backend.fetch_recent_updates(since_iso=...)` for the
   trailing 24h window.
2. Replays each `CRMUpdate` through `apply_inbound_update`. An update
   that matches the local snapshot is counted as
   `skipped_already_applied`; an update whose application cannot be
   resolved is counted as `unresolved`; an update that mutates state
   is counted as `applied`.
3. Emits a `crm_reconciliation_completed` outbox event whose payload
   carries the tally and the trailing-window bounds.

The job is **deterministic given a deterministic backend**: the
in-tree backends return `()` from `fetch_recent_updates`, so unit
tests inject a stub backend whose `fetch_recent_updates` yields the
fixture diff under examination.

`scheduled_jobs.crm_daily_reconciliation(db)` runs the reconciliation
once per configured backend. The cron expression is `0 7 * * *`
(07:00 UTC).

## Webhook signature schemes

| Provider | Header | Scheme |
| --- | --- | --- |
| Affinity | `Affinity-Webhook-Signature` | `hex(HMAC-SHA-256(secret, raw_body))`, optional `sha256=` prefix accepted |
| HubSpot  | `X-HubSpot-Signature-v3`     | `base64(HMAC-SHA-256(secret, raw_body))` |

Verification uses `hmac.compare_digest` and rejects empty secrets /
empty signatures. There is **no env-gated bypass** and **no dev-only
skip path** -- this is a load-bearing prompt-55 prohibition.

## Environment variables

| Name | Purpose |
| --- | --- |
| `AFFINITY_API_KEY` | Affinity REST API key (server-side only). |
| `AFFINITY_WEBHOOK_SECRET` | HMAC secret for inbound Affinity webhooks. |
| `AFFINITY_API_BASE` | Override for the Affinity API base URL. |
| `HUBSPOT_PRIVATE_APP_TOKEN` | HubSpot Private App access token. |
| `HUBSPOT_WEBHOOK_SECRET` | App secret for v3 webhook signature verification. |
| `HUBSPOT_API_BASE` | Override for the HubSpot API base URL. |

When a backend's required vars are missing, `from_env()` raises
`CRMConfigError` and the daily reconciliation job logs and skips that
backend.

## Prohibitions (load-bearing)

* CRM webhooks MUST NOT mutate `decision.verdict`. Verdicts are
  produced only by `decision_policy`.
* Webhook signatures MUST be verified before any state mutation.
* A null / missing field on the CRM side MUST NOT delete a local
  field.

## Events emitted

| Event type | Trigger |
| --- | --- |
| `crm_upsert_requested` | Outbound enqueue on application status / verdict change. |
| `crm_inbound_applied` | An inbound webhook actually changed the local mirror. |
| `crm_reconciliation_completed` | Daily reconciliation run finished (one per backend). |
