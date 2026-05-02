# API gateway: rate limit, CORS, request signing, request ID

This spec describes the four middleware layers introduced in prompt 37
that together form the public-edge gateway for the fund orchestrator
API. They live in `server/fund/middleware/` and are wired onto the
FastAPI app by `install_gateway_middleware`.

## Middleware order

Outermost to innermost on the inbound path:

1. `RequestIdMiddleware` — assigns or echoes `X-Request-ID`.
2. `CORSMiddleware` — installed by `install_cors`.
3. `RequestSigningMiddleware` — gates `/api/v1/internal/*`.
4. `RateLimitMiddleware` — token-bucket per API-key prefix or IP.
5. `FundSecurityMiddleware` (existing) — auth + per-route audit.

Outbound responses unwind in the reverse order, so the request id is
always available on a response — including 4xx denials produced by
deeper layers.

## Rate limiting

- Token bucket keyed on the **API-key prefix** when an `X-API-Key`
  (or `Authorization: Bearer …`) header is present, otherwise on the
  **client IP**.
- Refill rate is the `rate_limit_per_minute` of the resolved API-key
  row, falling back to `RATE_LIMIT_DEFAULT` (env
  `COHERENCE_FUND_RATE_LIMIT_DEFAULT`, default 120) for anonymous or
  pre-resolution requests.
- Backend: Redis (Lua token bucket) when reachable and the worker
  backend is `arq`; otherwise an in-process bucket. Redis failures
  silently degrade to the in-process bucket — a Redis blip never turns
  into a 5xx.
- Denials return `429 Too Many Requests` with a `Retry-After: <int>`
  header and JSON body `{"error": "rate_limited",
  "retry_after_seconds": <int>}`.
- `/health`, `/live`, `/ready`, `/docs`, `/openapi.json`, and CORS
  preflights are skipped.

## CORS

- Allow-list configured via `COHERENCE_FUND_CORS_ALLOWED_ORIGINS`
  (comma-separated). A literal `*` is allowed only when
  `settings.environment == "dev"`; in any other environment the
  installer raises at boot to fail loudly on misconfiguration.
- Methods: `GET, POST, PUT, DELETE, OPTIONS`.
- Allowed headers: `Authorization, Content-Type, X-Request-ID,
  X-Coherence-Signature, X-Coherence-Timestamp, X-API-Key`.
- Exposed headers: `X-Request-ID`.
- `allow_credentials=True`, `max_age=600`.

## Request signing

Required only on paths under `/api/v1/internal/*` — service-to-service
traffic. Two headers must be present:

- `X-Coherence-Timestamp` — RFC 3339 UTC.
- `X-Coherence-Signature` — `v1=<hex>`.

Canonical string:

    {ts}\n{METHOD}\n{path}\n{sha256_hex(body)}

Signed with HMAC-SHA-256 keyed on
`COHERENCE_FUND_REQUEST_SIGNING_SECRET`. The hash is **always
SHA-256** — MD5 and SHA-1 are not accepted. Only the `v1=` prefix is
recognised.

- **Skew window:** ±300 seconds (env
  `COHERENCE_FUND_REQUEST_SIGNING_MAX_SKEW_SECONDS`).
- **Replay protection:** bounded LRU of the most recent 10 000
  `(timestamp, signature)` pairs. A re-presentation of the same pair
  within the skew window returns 401.
- **Logging:** denied signatures are logged truncated to
  `v1=<first 8 chars>...`; the secret itself is never logged.
- **Misconfiguration:** if the secret is unset, internal routes return
  `503 signing_unconfigured` rather than silently allowing requests.

Failures return `401 invalid_signature` with a short message.

## Request ID

- Reads `X-Request-ID` from the caller; if absent, mints a UUID4 hex.
- Stores the id in `request.state.request_id` and a `ContextVar` so
  loggers can pick it up via `RequestIdLogFilter`.
- Echoes the id back on every response, including denials.

## Configuration summary

| Env var | Default | Notes |
| ------- | ------- | ----- |
| `COHERENCE_FUND_CORS_ALLOWED_ORIGINS` | (empty) | Comma-separated. `*` only valid in dev. |
| `COHERENCE_FUND_RATE_LIMIT_DEFAULT` | `120` | Per-minute fallback when no key row is resolved. |
| `COHERENCE_FUND_REQUEST_SIGNING_SECRET` | (empty) | Required for `/api/v1/internal/*`. |
| `COHERENCE_FUND_REQUEST_SIGNING_MAX_SKEW_SECONDS` | `300` | Skew window for signing timestamps. |
