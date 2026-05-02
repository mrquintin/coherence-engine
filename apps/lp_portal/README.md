# Coherence Fund — LP Portal

Next.js (App Router) frontend for verified limited partners. Pages:

- `/` — sign-in landing.
- `/lp` — LP overview (commitment, called, NAV, IRR).
- `/lp/statements` — quarterly NAV statements (PDFs sealed by content digest).
- `/lp/notices` — capital-call and distribution notices.

## RBAC

Every `/lp/*` page calls `requireLpSession()` (`src/lib/guard.ts`), which:

1. reads the Supabase Auth session,
2. requires `app_metadata.roles` to include `"lp"` (or the legacy `app_metadata.role === "lp"`),
3. requires `app_metadata.lp_id` to be set, and
4. constructs an `LpApiClient` keyed off the session's own `lp_id` —
   no user-supplied `lp_id` is ever honoured by the helper.

The backend additionally enforces row-level security on the LP statement
and notice tables; the portal RBAC is the first of two layers, not the
only one.

## Local development

```
pnpm install
cp .env.example .env.local   # populate Supabase project values
pnpm dev                     # http://localhost:3003
```

## Tests

```
pnpm tsc --noEmit
pnpm test                    # vitest run
```
