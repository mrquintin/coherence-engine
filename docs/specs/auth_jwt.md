# Supabase Auth — JWT verification contract

Status: implemented in prompt 25.

This spec defines how the fund backend verifies Supabase-issued JWTs from
the founder portal, how it maps token identity onto the `Founder` row, and
how the JWKS cache rotates keys without a service restart.

## Token shape

Founder-portal tokens are minted by Supabase Auth and arrive in the
`Authorization: Bearer <jwt>` header. The backend treats the following
claims as the contract:

| Claim    | Required | Notes                                                       |
|----------|----------|-------------------------------------------------------------|
| `sub`    | yes      | Supabase user ID (UUID-shaped). Maps to `founder_user_id`. |
| `aud`    | yes      | Must equal `SUPABASE_JWT_AUD` (default `authenticated`).   |
| `iss`    | optional | If `SUPABASE_JWT_ISS` is set, must match exactly.          |
| `exp`    | yes      | Unix timestamp; rejected past expiry (30s clock skew).     |
| `nbf`    | optional | Honored with the same 30s skew.                             |
| `email`  | optional | Used as fallback profile data on lazy founder upsert.      |

## Signing algorithms

* Accepted: `RS256`, `RS384`, `RS512`, `ES256`, `ES384` (asymmetric).
* **Rejected:** `HS256` and any other HMAC variant. Supabase issues
  asymmetric tokens for end-user API access; if we accepted HMAC, a leaked
  shared secret would let any caller mint admin tokens.
* Algorithm is read from the JWT header and validated against the
  allowlist before key lookup.

## JWKS retrieval and rotation

The verification public keys are fetched from the URL given by
`SUPABASE_JWKS_URL` (typically `${SUPABASE_URL}/auth/v1/.well-known/jwks.json`).

* **Cold start:** first request fetches and caches the JWKS document.
* **Warm hits:** subsequent requests serve from cache for
  `JWKS_CACHE_TTL_SECONDS` (default 3600).
* **Key rotation:** when the JWT header carries an unknown `kid`, the cache
  triggers a refresh — rate-limited to one refresh per `kid` per 30 seconds
  so a malicious caller cannot pin the service to its JWKS endpoint by
  emitting random `kid` values.
* **Stale-cache fallback:** if a refresh attempt fails but cached keys are
  still present, we serve from the stale cache and log a warning. New
  founder traffic continues to verify until the next successful refresh.
* **JWKS unavailable:** if the cache is empty *and* the JWKS endpoint is
  unreachable, the auth dependency raises `JWKSUnavailable` and the
  request is mapped to **HTTP 503** — not 500. This signals operators
  that the dependency, not the service code, is down.

## Failure mapping

| Condition                                  | Status | Code                 |
|-------------------------------------------|--------|----------------------|
| Missing / malformed `Authorization` header | 401    | `UNAUTHORIZED`       |
| Signature invalid / tampered               | 401    | `UNAUTHORIZED`       |
| `exp` in the past (with 30s skew)          | 401    | `UNAUTHORIZED`       |
| Disallowed signing algorithm (e.g. HS256)  | 401    | `UNAUTHORIZED`       |
| Wrong `aud`                                | 403    | `FORBIDDEN`          |
| Wrong `iss`                                | 403    | `FORBIDDEN`          |
| JWKS unreachable, no cached keys           | 503    | `JWKS_UNAVAILABLE`   |

## Founder identity mapping

`Founder.founder_user_id` (added in alembic
`20260425_000002_founder_user_id`) carries the JWT `sub` claim.

* **First call:** `current_founder` lazily upserts a `Founder` row with
  `founder_user_id = sub` and `email = email_claim`. Profile fields
  (`full_name`, `company_name`, `country`) start empty and are filled on
  the next `POST /applications`.
* **Subsequent calls:** the row is fetched by `founder_user_id` index.
* **Concurrency:** the upsert flushes inside the request transaction; on
  unique-constraint conflict (e.g. parallel first-call from the same
  user) we re-read the existing row.

The migration adds the column nullable per the
expand/backfill/contract pattern (prompt 24); pre-existing founders
remain unlinked until they sign in via the portal.

## Defense-in-depth ownership check

Even with Postgres RLS enforcing `jwt_sub = founder_user_id`, the API-key
service-role path bypasses RLS. Each founder-scoped route therefore also
checks `application.founder_id == current_founder.id` before returning
data. RLS is the floor; the application check is the ceiling. The policy
reads `jwt_sub` from PostgREST request settings rather than relying on
`auth.uid()` schema visibility, which lets the app-owned migration role
install the policy on Supabase.

## Layering with the existing API-key middleware

`FundSecurityMiddleware` continues to gate service-role traffic (workers,
admin tooling) by API key. The new `current_founder` dependency adds a
JWT layer on the founder-portal routes. The dependency is dual-mode:

* If the request carries `Authorization: Bearer <jwt>`, the JWT is verified
  and the founder is returned.
* If no Bearer token is present *and* the middleware has already
  authenticated an API-key principal with role `admin`, `analyst`, or
  `viewer`, the dependency returns `None` and the route handler skips
  the ownership check (service-role bypass).
* Otherwise the dependency returns 401.

## Health probe posture

| Path                | Auth required | Notes                            |
|---------------------|---------------|----------------------------------|
| `GET /healthz`      | none          | Liveness; no DB, no JWKS.        |
| `GET /readyz`       | none          | DB + JWKS reachability check.    |
| `GET /health`       | none          | Legacy alias.                    |
| `GET /ready`        | none          | Legacy alias (DB only).          |

Probes intentionally live outside the `/applications` and `/admin`
namespaces; the middleware short-circuits non-fund paths with no
allowlist edit required.

## Founder-portal client integration

The Next.js founder portal obtains the JWT from `supabase-js`:

```ts
import { createBrowserClient } from "@supabase/ssr";

const supabase = createBrowserClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);

const { data: { session } } = await supabase.auth.getSession();
const token = session?.access_token;

await fetch(`${API_BASE}/api/v1/applications`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Idempotency-Key": crypto.randomUUID(),
    "Content-Type": "application/json",
  },
  body: JSON.stringify(payload),
});
```

The portal must refresh the session before calling the API when
`session.expires_at` is within the 30s clock skew — otherwise `supabase-js`
already returns a fresh token via its background refresh.

## Operational checklist

1. Set `SUPABASE_JWKS_URL`, `SUPABASE_JWT_AUD`, `SUPABASE_JWT_ISS` in the
   service environment.
2. Confirm `/readyz` returns 200 — both DB and JWKS reachable.
3. After Supabase rotates a signing key, expect one cache-miss latency
   bump (one HTTP fetch) per backend instance, then steady state.
4. If `/readyz` reports `jwks: unreachable`, founder-portal traffic is
   already failing 503 — page the on-call on the *Supabase auth*
   pager, not the service one.
