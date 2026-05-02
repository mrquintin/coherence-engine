# Feature flags

## Why we have one

The pre-seed pipeline ships behavior changes behind feature flags so we
can land code incrementally, A/B-test scoring tweaks against a shadow
cohort, and roll back individual decisions without redeploying. Until
prompt 32, gates were ad-hoc `if config.x:` reads scattered across the
codebase. This module is the single replacement.

## Flag registry

`data/governed/feature_flags.yaml` is the authoritative declaration.
It is the *only* place a flag is born. The runtime refuses to start
if the file's `schema_version` is not `feature-flags-v1`, if any flag
declares an invalid default, or if a flag is both `restricted: true`
and `client_visible: true` (a combination that would leak decision
policy state to the front end).

Every entry has:

| field | purpose |
| --- | --- |
| `key` | dotted lower-snake identifier. Unique. |
| `type` | one of `boolean`, `string-enum`, `int-percent`. |
| `default` | value used when no backend resolves the flag. |
| `restricted` | true iff flipping the flag changes decision-policy semantics. |
| `client_visible` | true iff the flag is allowed on `/api/v1/flags/public`. |
| `owner` | team responsible for the flag's lifecycle. |
| `description` | one-line rationale. |
| `enum` | (string-enum only) allowed string values. |

## Resolution order

`FeatureFlags.get_bool / get_string / get_percent` consult, in order:

1. The configured backend (`LaunchDarklyBackend`, `PostHogBackend`).
   `COHERENCE_FUND_FEATURE_FLAGS_BACKEND=launchdarkly|posthog|local`
   selects it; the default `local`/`null` bypasses any remote call.
   Backend SDKs are imported lazily, so neither is a hard dependency.
2. Any in-memory YAML override applied by `flags set` in the current
   process.
3. The YAML default declared in the registry.
4. The caller-supplied default (`get_bool("k", default=True)`).
5. `MissingFlag` is raised. The registry is authoritative; unknown
   keys are bugs, not configuration mistakes.

Resolved values are cached for 60 seconds (configurable via the
`cache_ttl_seconds` ctor argument). Any `flags set` invocation calls
`invalidate_cache()` so the change is visible on the next read.

Type coercion lives on `FlagSpec.coerce`:

* `boolean` accepts `True`/`False`, the strings `true|false|1|0|yes|no|on|off`,
  and integers (`0` -> False, anything else -> True).
* `string-enum` accepts any string in the declared `enum`. Backend
  values outside the enum are *ignored* (logged + fall through to YAML)
  rather than raising — a misconfigured remote flag must not break the
  pipeline.
* `int-percent` accepts `int` or numeric string in `[0, 100]`.

## Restricted flags and the audit invariant

A flag is `restricted` when flipping it would change the answer the
decision policy returns for a given input. Examples in the seed
registry: `anti_gaming.enabled`, `decision_policy.shadow_mode`,
`scoring.uncertainty_floor`. These flags can only be flipped through
`FeatureFlags.set_restricted(key, value, actor=..., reason=...)`,
which:

1. Coerces and validates the new value against the flag's declared type.
2. Writes a JSONL audit row to `data/governed/feature_flag_audit.log`
   containing `{audit_id, occurred_at, key, flag_type, old_value,
   new_value, actor, source, reason}`.
3. Emits a `decision_policy_flag_changed.v1` event (schema:
   `server/fund/schemas/events/feature_flag_changed.v1.json`).

The event is what makes backtests reproducible: a replay against a
historical decision can recover the flag state that was in effect at
decision time by walking `decision_policy_flag_changed` events
backwards from "now".

The audit invariant is *load-bearing*: `FeatureFlags` raises
`RestrictedFlagViolation` if no `event_publisher` is wired in. Two
emitter implementations ship today:

* `EventPublisher.publish_decision_policy_flag_changed(audit_row)` —
  DB-backed, writes to the standard `EventOutbox` so dispatchers
  forward it to Kafka/SQS/Redis like every other event.
* `feature_flags.jsonl_event_emitter(log_path)` — file-backed,
  appends a fully formed event envelope to a JSONL file. Used by the
  CLI when no DB session is available.

Mutation of restricted flags via `set_unrestricted` raises; the
inverse also raises (defense in depth — the registry already enforces
that restricted flags cannot be `client_visible`, but the runtime
guards both directions).

## CLI

```sh
# List every registered flag with type, current value, and source.
python -m coherence_engine flags list
python -m coherence_engine flags list --format json

# Flip a non-restricted flag (local YAML override; non-prod only).
python -m coherence_engine flags set --key ui.investor_portal_beta --value true

# Flip a restricted flag — actor + reason required, audit row + event emitted.
python -m coherence_engine flags set \
    --key anti_gaming.enabled \
    --value false \
    --actor "ops@coherence" \
    --reason "investigating false-positive incident INC-204"
```

`flags list` prints `KEY  TYPE  VALUE  SOURCE  RESTRICTED`. The
`SOURCE` column is the layer that actually returned the value: one of
`launchdarkly`, `posthog`, `yaml`. In CI/dev, where no backend is
configured, every row reads `yaml`.

## Consuming flags from Python

```python
from coherence_engine.server.fund.services.feature_flags import get_feature_flags

flags = get_feature_flags()
if flags.get_bool("anti_gaming.enabled"):
    score = anti_gaming.adjust(score)

engine = flags.get_string("backtest.replay_engine")  # 'deterministic' | 'probabilistic'
floor = flags.get_percent("scoring.uncertainty_floor")
```

Tests should never call `get_feature_flags()` — they should construct
a `FeatureFlags(registry_path=..., backend=stub, event_publisher=stub)`
instance scoped to the test, so cache and overrides do not leak across
tests.

## Consuming flags from Next.js

Public flags (those with `client_visible: true` *and* not
`restricted`) are served by `GET /api/v1/flags/public`. The handler
returns a flat `{ "<key>": <value> }` map, suitable for a React
context provider:

```ts
const res = await fetch("/api/v1/flags/public");
const flags = await res.json();
if (flags["ui.investor_portal_beta"]) {
  // render the beta surface
}
```

`/api/v1/flags/public` is intentionally narrow — it never includes
restricted flags, owners, descriptions, or the source layer. Front
ends that need richer metadata go through the admin UI, which calls
`FeatureFlags.list_flags()` on the server.

## What lives outside this module

* The actual decision-policy code reads flags through
  `FeatureFlags.get_*` rather than baking thresholds into config. The
  thresholds themselves remain in `FundSettings` /
  `decision_policy.py`.
* Per-tenant or per-user targeting is delegated to the configured
  backend. The `LaunchDarklyBackend` / `PostHogBackend` accept a
  `user_key` / `distinct_id` so future call sites can bind the flag
  context to the application or founder identity. The default
  service-level identity is `service-default`.
