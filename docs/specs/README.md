# Technical Spec Pack (Execution Ready)

This folder contains implementation-grade contracts for immediate engineering kickoff.

## Contents

- `decision_policy_spec.md`
  - deterministic equations, gate logic, parameters, and reason codes
- `event_schemas.md`
  - canonical async event definitions and producer/consumer mapping
- `api_contracts_v1.md`
  - first REST contract set with request/response examples and endpoint-level role ownership matrix
- `openapi_v1.yaml`
  - machine-readable API contract (OpenAPI 3.1) including `x-required-roles` ownership annotations
- `STARTER_FASTAPI_SCAFFOLD.md`
  - how to run the generated scaffold, migrations, and outbox worker
- `schemas/envelope.schema.json`
  - shared event envelope schema
- `schemas/events/*.json`
  - v1 payload schemas for initial event set

## Recommended Build Order

1. Implement event envelope validation and dead-letter handling.
2. Implement `/applications` and `/applications/{id}/interview-sessions`.
3. Implement `/applications/{id}/score` and async job orchestration.
4. Implement decision engine using `decision_policy_spec.md`.
5. Implement `/applications/{id}/decision` and escalation endpoint.

