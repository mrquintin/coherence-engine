# Voice intake — Twilio phone-based founder interview ingress (prompt 38)

## Overview

The voice intake path is an alternative to the web form / chat ingress: a
founder dials (or is dialed by) a Twilio number, the call is driven by a
TwiML script generated from the prompt registry (prompt 08), each topic's
answer is recorded and persisted to object storage, and a single
`interview_session_completed` event is emitted when the call ends.

The components are:

| File | Role |
| --- | --- |
| `server/fund/services/twilio_adapter.py` | `RequestValidator`, signature verification, `TwilioClient` protocol, test seam. |
| `server/fund/services/voice_intake.py` | `start_call`, TwiML rendering, `store_recording`, `finalize_session`. |
| `server/fund/routers/twilio_webhooks.py` | `/webhooks/twilio/voice`, `/recording`, `/status`. |
| `server/fund/schemas/events/interview_session_completed.v1.json` | Outbox event schema. |
| `alembic/versions/20260425_000006_interview_recordings.py` | `fund_interview_recordings` table. |

## Call lifecycle

1. Operator (or the founder portal) calls
   `voice_intake.start_call(application_id, phone_number, ...)`.
   - Persists an `InterviewSession` row (`channel="voice"`).
   - Calls `TwilioClient.place_call` which dials the founder. The
     `twiml_url` and `status_callback_url` carry `?session_id=<id>` so
     subsequent webhooks can correlate without relying on Twilio's
     `CallSid` (not known until the call has been queued).
2. Twilio dials the founder and POSTs to `POST /webhooks/twilio/voice`.
   The handler renders the greeting + first topic prompt + first
   `<Record>`. The `<Record>` `action` URL points at
   `/webhooks/twilio/recording?session_id=<id>&topic_id=<topic>`.
3. After each recording finishes Twilio POSTs the recording URL +
   duration + `RecordingSid` to `/recording`. The handler:
   - Verifies the signature.
   - Authenticated-fetches the recording bytes via `TwilioClient.fetch_recording`.
   - Stores them through the object-storage adapter
     (`coh://<backend>/<bucket>/interviews/<application_id>/<session_id>/<topic_id>.wav`).
   - Writes an `InterviewRecording` row (uri, sha256, duration, sid).
   - Returns the next topic's TwiML, or — if every topic is recorded —
     a final farewell + `<Hangup/>`.
4. Twilio POSTs call-status updates to `/webhooks/twilio/status`.
   When the status reaches a terminal value (`completed`, `failed`,
   `no-answer`, `canceled`, `busy`) the handler calls
   `voice_intake.finalize_session`, which emits exactly one
   `interview_session_completed` event and marks the session
   `completed`. Subsequent terminal-status callbacks are no-ops.

## Signature verification

Every Twilio webhook is gated by an HMAC-SHA1 signature check
(`twilio_adapter.RequestValidator`). The validator computes:

```
signature = base64(hmac_sha1(TWILIO_AUTH_TOKEN, url + sorted(form_param k+v)))
```

Mismatch → `401 UNAUTHORIZED`. Empty token or empty signature also fails.

`TWILIO_VALIDATE_WEBHOOK_SIGNATURE=false` may only opt out of the check
when `COHERENCE_FUND_ENV=dev`. In `staging` and `prod` the env var is
ignored. The check is implemented in
`twilio_webhooks._signature_validation_required` and reads `is_dev()`
from `services.env_gates`.

The signed URL is the *public* URL Twilio reached. Behind a load
balancer or tunnel, the handler reconstructs it from
`X-Forwarded-Proto` / `X-Forwarded-Host` rather than trusting
`request.url` (which would point at the internal hostname).

## Topics & determinism

Topics are sourced from `data/prompts/registry.json` (prompt 08). Only
entries with `status == "prod"` and a registered voice line in
`voice_intake._TOPIC_VOICE_LINES` are surfaced. The result is a
`tuple[InterviewTopic]` so the ordering is stable; the rendered TwiML
is asserted against a pinned snapshot in `tests/test_voice_intake.py`.

If you change the rendering, update the snapshot deliberately.

## Storage discipline

* Recording bytes go through the object-storage adapter — never the
  database. The DB column `recording_uri` is the canonical URI; the
  bytes themselves live behind it.
* `recording_sha256` is recomputed by the storage adapter on write
  and compared to the pre-put hash; mismatch raises
  `StorageHashMismatch` and the webhook returns 5xx (Twilio retries).

## Twilio number provisioning

1. Buy or port a number in the Twilio Console with **Voice** enabled.
2. Configure the number's "A call comes in" webhook to:
   `https://<public-host>/api/v1/webhooks/twilio/voice` (POST).
3. Configure the "Status callback URL" to:
   `https://<public-host>/api/v1/webhooks/twilio/status` (POST).
4. Set `TWILIO_FROM_NUMBER` to the same E.164 number; outbound calls
   placed via `TwilioClient.place_call` use it as caller-id.
5. Cross-link: the infrastructure runbook for SSL termination,
   subdomain CNAMEs, and the IP-allowlist enrolment for outbound
   Twilio webhooks lives in `docs/ops/twilio-infra-setup.pdf`
   (operator-only — request access from `#fund-ops`).

## Cost envelope (per-minute pricing)

Twilio bills per inbound + outbound minute (US-domestic ≈ $0.014/min
inbound to a long-code number, ≈ $0.013/min outbound dial; recording
storage adds $0.0025 per recording per month). A 10-minute interview
across two topics costs ≈ $0.30 in voice + $0.005 in recording
storage. Prompt 62 (cost telemetry) wires the per-call dollar amount
into the per-application unit-economics dashboard; the metadata it
needs (call duration, recording count) is already recorded on the
`InterviewRecording` rows.

## Testing

* `tests/test_voice_intake.py` — service-layer tests with an injected
  fake `TwilioClient`. Covers topic loading, TwiML pinned snapshot,
  storage round-trip, and idempotent `finalize_session`.
* `tests/test_twilio_webhooks.py` — FastAPI `TestClient` integration
  tests with mocked signature, recording fetch, and status
  callbacks. Exercises both the valid-signature happy path and the
  invalid-signature 401 path.

Tests **must not** call the real Twilio API. The
`set_twilio_client_for_tests` and `set_recording_fetcher_for_tests`
seams exist for hermetic substitution.
