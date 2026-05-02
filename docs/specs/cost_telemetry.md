# Cost telemetry & budget alerts (prompt 62)

## Purpose

Track every paid external call (LLM tokens, STT minutes, embeddings,
Twilio call minutes, Stripe fees) per application. The data feeds:

* Per-application cost rollup on the partner-dashboard cost view.
* Daily / per-application budget alerts that emit a
  `cost_budget_exceeded.v1` outbox event when a configured cap is
  crossed.
* The model-risk report (prompt 60) -- cost-per-decision is one of
  its KPIs.

## Data model

`fund_cost_events`:

| column            | notes                                              |
| ----------------- | -------------------------------------------------- |
| id                | `cst_<hex>` primary key                            |
| application_id    | nullable -- cross-cutting infra cost is allowed    |
| provider          | operator-readable provider name                    |
| sku               | pricing-registry key                               |
| units             | server-observed unit count (minutes, 1k tokens)    |
| unit              | unit string mirrored from the pricing entry        |
| unit_cost_usd     | derived from `data/governed/cost_pricing.yaml`     |
| total_usd         | `units * unit_cost_usd`                            |
| idempotency_key   | unique index, dedupes webhook retries              |
| occurred_at       | when the upstream call actually happened           |

`fund_cost_alert_state`:

| column          | notes                                              |
| --------------- | -------------------------------------------------- |
| scope           | `application` or `daily`                           |
| scope_key       | application id or `YYYY-MM-DD`                     |
| last_alert_at   | timestamp of the most recent emitted alert        |
| last_total_usd  | the rolled-up total at emit-time                  |

A unique index on `(scope, scope_key)` makes the cooldown ledger
lookup O(1).

## Pricing registry

`data/governed/cost_pricing.yaml` (`schema_version: cost-pricing-v1`):

```yaml
prices:
  - sku: deepgram.nova-2.audio_minute
    unit: minute
    unit_cost_usd: 0.0043
  - sku: openai.text-embedding-3-large.tokens
    unit: 1000_tokens
    unit_cost_usd: 0.00013
  - sku: twilio.voice.outbound_us
    unit: minute
    unit_cost_usd: 0.013
```

Operator obligation: vendor prices change. Any edit MUST go through
the standard governance review (changelog + reviewer signoff). The
`schema_version` pin makes a forward-incompatible registry fail loud
at load time.

## API

```python
from coherence_engine.server.fund.services.cost_telemetry import (
    record_cost, compute_idempotency_key,
)

record_cost(
    db,
    provider="deepgram",
    sku="deepgram.nova-2.audio_minute",
    units=10.0,                    # MINUTES, server-observed
    application_id="app_abc",
    idempotency_key=compute_idempotency_key(
        provider="deepgram",
        sku="deepgram.nova-2.audio_minute",
        application_id="app_abc",
        discriminator=deepgram_request_id,
    ),
)
```

### Wired-in call sites

* `voice_intake.finalize_session` -- emits one Twilio cost row per
  session, using the sum of stored `InterviewRecording.duration_seconds`
  (NOT a caller-supplied minute count).
* `scoring.record_scoring_cost` -- helper called by ApplicationService
  after each scoring run to record the embedding-token bucket.
* `stt.router.record_stt_cost` -- helper called by the STT caller
  after a successful transcription, using the recording's persisted
  duration.

### Budget alerts

```python
from coherence_engine.server.fund.services.cost_alerts import (
    check_application_budget, check_daily_budget,
)

decision = check_application_budget(db, application_id)
# decision.exceeded     -- total > MAX_COST_PER_APPLICATION_USD
# decision.alert_emitted -- True if a fresh outbox event was written
# decision.cooldown_active -- True if cooldown blocked the emit
```

Both check functions emit a `cost_budget_exceeded.v1` outbox event
when the threshold is crossed AND the cooldown for that
`(scope, scope_key)` pair has elapsed. Cooldown defaults to 24h
(`COST_ALERT_COOLDOWN_HOURS`). Routing of the alert reuses the
prompt 14 notifications service via the outbox dispatcher.

## Configuration

| env var                          | default | meaning                              |
| -------------------------------- | ------- | ------------------------------------ |
| `MAX_COST_PER_APPLICATION_USD`   | 50.0    | per-application alert threshold      |
| `MAX_COST_PER_DAY_USD`           | 500.0   | per-day alert threshold              |
| `COST_ALERT_COOLDOWN_HOURS`      | 24      | minimum gap between alerts per scope |

## Prohibitions (prompt 62)

1. **Never trust client-supplied unit counts.** Units are computed
   from the recording duration we persisted, the SDK response usage
   block, or the upstream's webhook -- never from a value supplied
   by the founder portal or a partner UI.
2. **Never alert without cooldown.** A runaway counter must not
   page the operator every minute. The cooldown ledger
   (`fund_cost_alert_state`) is the single source of truth.
3. **Never hardcode pricing.** All prices live in
   `data/governed/cost_pricing.yaml`. The loader fails loud on a
   schema-version mismatch or an unknown SKU.
