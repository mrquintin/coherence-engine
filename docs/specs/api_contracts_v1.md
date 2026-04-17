# API Contracts (v1)

## Purpose

Define first implementation-ready synchronous API contracts for intake, scoring orchestration, decision retrieval, and escalation workflows.

Base URL (example): `/api/v1`

---

## Common Conventions

Headers:

- `Content-Type: application/json`
- `X-Request-Id: <uuid>`
- `Idempotency-Key: <string>` (required for POST mutating endpoints)
- `X-API-Key: <secret>` or `Authorization: Bearer <secret>` (required for protected fund routes)

Response envelope:

```json
{
  "data": {},
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

Error envelope:

```json
{
  "data": null,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "requested_check_usd must be greater than 0",
    "details": [{"field": "requested_check_usd", "issue": "must be > 0"}]
  },
  "meta": {"request_id": "req_01J..."}
}
```

Role ownership matrix (endpoint-level):

| Endpoint | Method | Required role(s) |
|---|---|---|
| `/health` | GET | public (no auth) |
| `/live` | GET | public (no auth) |
| `/ready` | GET | public (no auth) |
| `/secret-manager/ready` | GET | public (no auth) |
| `/applications` | POST | analyst, admin |
| `/applications/{application_id}/interview-sessions` | POST | analyst, admin |
| `/applications/{application_id}/score` | POST | analyst, admin |
| `/applications/{application_id}/decision` | GET | viewer, analyst, admin |
| `/applications/{application_id}/escalation-packet` | POST | admin |
| `/admin/api-keys` | POST | admin |
| `/admin/api-keys` | GET | admin |
| `/admin/api-keys/{key_id}/revoke` | POST | admin |
| `/admin/api-keys/{key_id}/rotate` | POST | admin |

Auth mode note:
- In production posture (`COHERENCE_FUND_AUTH_MODE=db`), the role table above is strictly enforced.
- In local integration mode (`COHERENCE_FUND_AUTH_MODE=disabled`), middleware injects an `admin` principal for compatibility.

---

## 1) Create Application Intake

`POST /applications`

Required role(s): `analyst` or `admin`

Creates a new founder application and returns IDs for downstream interview orchestration.

Request:

```json
{
  "founder": {
    "full_name": "Jane Founder",
    "email": "jane@example.com",
    "company_name": "Acme Labs",
    "country": "US"
  },
  "startup": {
    "one_liner": "AI copilot for logistics procurement",
    "requested_check_usd": 250000,
    "use_of_funds_summary": "Hire 2 engineers and run pilots",
    "preferred_channel": "web_voice"
  },
  "consent": {
    "ai_assessment": true,
    "recording": true,
    "data_processing": true
  }
}
```

Response `201`:

```json
{
  "data": {
    "application_id": "app_01J...",
    "founder_id": "fnd_01J...",
    "status": "intake_created"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## 2) Start Interview Session

`POST /applications/{application_id}/interview-sessions`

Required role(s): `analyst` or `admin`

Creates a session token and voice routing metadata.

Request:

```json
{
  "channel": "phone|web_voice|async_voice",
  "locale": "en-US"
}
```

Response `201`:

```json
{
  "data": {
    "interview_id": "ivw_01J...",
    "session_token": "tok_...",
    "routing": {
      "phone_number": "+15551234567",
      "webrtc_room_url": "https://voice.example.com/room/ivw_01J..."
    }
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## 3) Trigger Scoring Pipeline

`POST /applications/{application_id}/score`

Required role(s): `analyst` or `admin`

Triggers orchestration for transcript compilation, domain profiling, coherence scoring, and decision policy evaluation.

Request:

```json
{
  "mode": "standard|priority",
  "dry_run": false,
  "transcript_text": "optional raw transcript text",
  "transcript_uri": "optional object storage URI"
}
```

Response `202`:

```json
{
  "data": {
    "job_id": "job_01J...",
    "status": "queued"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## 4) Get Application Decision

`GET /applications/{application_id}/decision`

Required role(s): `viewer`, `analyst`, or `admin`

Returns latest decision artifact if available.

Response `200`:

```json
{
  "data": {
    "application_id": "app_01J...",
    "decision_id": "dec_01J...",
    "decision": "pass|fail|manual_review|pending",
    "policy_version": "decision-policy-v1.0.0",
    "threshold_required": 0.27,
    "coherence_observed": 0.22,
    "margin": -0.05,
    "failed_gates": [
      {"gate": "coherence_gate", "reason_code": "COHERENCE_BELOW_THRESHOLD"}
    ],
    "updated_at": "2026-04-07T20:30:00Z"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## 5) Create Escalation Packet

`POST /applications/{application_id}/escalation-packet`

Required role(s): `admin`

Creates internal partner memo and founder handoff package. Allowed only when decision is `pass`.

Request:

```json
{
  "partner_email": "investments@example.com",
  "include_calendar_link": true
}
```

Response `201`:

```json
{
  "data": {
    "packet_id": "pkt_01J...",
    "packet_uri": "s3://bucket/escalations/pkt_01J...md",
    "status": "sent"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## 6) Health and Readiness

`GET /health`

Response `200`:

```json
{
  "data": {
    "status": "ok",
    "service": "fund-orchestrator-api",
    "version": "0.1.0"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

`GET /live`

Response `200`:

```json
{
  "data": {
    "status": "alive",
    "service": "fund-orchestrator-api",
    "version": "0.1.0"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

`GET /ready`

Response `200|503`:

```json
{
  "data": {
    "status": "ready",
    "database": "ok",
    "service": "fund-orchestrator-api"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

`GET /secret-manager/ready`

Returns startup probe + current provider status for secret-manager reachability.

Response `200|503`:

```json
{
  "data": {
    "status": "ready|configured|disabled|failed|unknown",
    "provider": "aws|gcp|vault|disabled",
    "reachable": true,
    "detail": "secret_ref reachable (coherence/fund/bootstrap-admin)",
    "checked_at": "2026-04-10T00:00:00+00:00",
    "service": "fund-orchestrator-api"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## 7) Admin API Key Lifecycle

These endpoints support operational key management with DB-backed records
(`active/inactive`, expiry, revoke/rotate) and audit events.

All endpoints below require `admin` role.

### Create API Key

`POST /admin/api-keys`

Request:

```json
{
  "label": "worker-redis-prod",
  "role": "viewer|analyst|admin",
  "expires_in_days": 30,
  "write_to_secret_manager": true,
  "secret_ref": "coherence/fund/worker-redis-prod"
}
```

Response `201`:

```json
{
  "data": {
    "id": "key_01J...",
    "token": "cfk_...",
    "label": "worker-redis-prod",
    "role": "analyst",
    "is_active": true,
    "expires_at": "2026-05-10T00:00:00+00:00",
    "fingerprint": "a1b2c3d4e5f6",
    "created_at": "2026-04-10T00:00:00+00:00"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

### List API Keys

`GET /admin/api-keys`

Response `200`:

```json
{
  "data": {
    "keys": [
      {
        "id": "key_01J...",
        "label": "worker-redis-prod",
        "role": "analyst",
        "is_active": true,
        "fingerprint": "a1b2c3d4e5f6",
        "created_by": "admin",
        "created_at": "2026-04-10T00:00:00+00:00",
        "expires_at": "2026-05-10T00:00:00+00:00",
        "revoked_at": null,
        "last_used_at": "2026-04-10T00:30:00+00:00"
      }
    ]
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

### Revoke API Key

`POST /admin/api-keys/{key_id}/revoke`

Response `200`:

```json
{
  "data": {"key_id": "key_01J...", "status": "revoked"},
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

### Rotate API Key

`POST /admin/api-keys/{key_id}/rotate`

Request:

```json
{
  "expires_in_days": 30,
  "write_to_secret_manager": true,
  "secret_ref": "coherence/fund/worker-redis-prod"
}
```

Response `201`:

```json
{
  "data": {
    "id": "key_01J_new...",
    "token": "cfk_...",
    "label": "worker-redis-prod-rotated",
    "role": "analyst",
    "is_active": true,
    "expires_at": "2026-05-10T00:00:00+00:00",
    "fingerprint": "c7d8e9f0a1b2",
    "created_at": "2026-04-10T00:00:00+00:00"
  },
  "error": null,
  "meta": {"request_id": "req_01J..."}
}
```

---

## Error Codes

- `VALIDATION_ERROR` -> `400`
- `UNAUTHORIZED` -> `401`
- `FORBIDDEN` -> `403`
- `NOT_FOUND` -> `404`
- `CONFLICT` -> `409`
- `UNPROCESSABLE_STATE` -> `422`
- `RATE_LIMITED` -> `429`
- `INTERNAL_ERROR` -> `500`

---

## State Machine (Application)

Allowed statuses:

- `intake_created`
- `interview_in_progress`
- `interview_completed`
- `scoring_queued`
- `scoring_in_progress`
- `decision_pending`
- `decision_pass`
- `decision_fail`
- `manual_review`
- `escalated`

Transition failures must return `409 CONFLICT`.

