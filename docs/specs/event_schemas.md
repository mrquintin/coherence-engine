# Fund Pipeline Event Schemas (v1)

This document is the authoritative index of canonical event schemas published by the
fund pipeline. The JSON Schema files live in `server/fund/schemas/events/` and are
loaded by `server/fund/services/event_schemas.py`.

Every event object shares a common envelope plus event-specific required fields:

| Envelope field | Type | Notes |
| --- | --- | --- |
| `event_id` | string (uuid) | Unique per event |
| `event_name` | string (const) | Matches the event identifier (see table below) |
| `schema_version` | integer (const `1`) | Schema major version |
| `occurred_at` | string (ISO-8601) | UTC timestamp |
| `application_id` | string | Target application identifier |

Schemas use JSON Schema Draft 2020-12 and declare `additionalProperties: false`.

## Event Index

| Event | Version | Schema path | Event-specific required fields |
| --- | --- | --- | --- |
| `interview_completed` | 1 | `server/fund/schemas/events/interview_completed.v1.json` | `session_id`, `transcript_ref`, `duration_s`, `asr_confidence_avg` |
| `argument_compiled` | 1 | `server/fund/schemas/events/argument_compiled.v1.json` | `argument_graph_ref`, `n_propositions`, `n_relations` |
| `decision_issued` | 1 | `server/fund/schemas/events/decision_issued.v1.json` | `decision` (`pass`\|`reject`\|`manual_review`), `cs_superiority`, `cs_required`, `decision_policy_version`, `scoring_version` |
| `founder_notified` | 1 | `server/fund/schemas/events/founder_notified.v1.json` | `channel` (`email`\|`sms`\|`dry_run`), `template_id`, `notification_status` (`queued`\|`sent`\|`failed`\|`suppressed`) |

## Validation

`server/fund/services/event_schemas.py` exposes:

- `SUPPORTED_EVENTS: dict[str, list[str]]` — name → supported versions.
- `load_schema(event_name, version="1") -> dict` — loads and caches the schema JSON.
- `validate_event(event_name, payload, version="1") -> None` — validates with
  `jsonschema.Draft202012Validator` when available, or a required-keys fallback
  otherwise. Raises `EventValidationError` on any failure.

`EventPublisher.publish` validates every event before outbox enqueue. Strict mode is
controlled by `COHERENCE_FUND_STRICT_EVENTS` (default `true`). In lenient mode
(`false`), validation failures are logged and the event is enqueued anyway.

Each schema file also ships a `examples` array containing at least one valid payload
for regression fixtures and documentation.
