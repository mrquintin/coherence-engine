# Coherence Fund — Founder Portal

Next.js 14 (App Router, React Server Components) frontend that lets founders
submit a pre-seed application to the Coherence Fund and check the resulting
decision. Authenticates against Supabase Auth (`@supabase/ssr`) and calls the
FastAPI backend through a TypeScript SDK regenerated from
`docs/specs/openapi_v1.yaml`.

## Stack

- Next.js `14.2.x` (App Router only; `pages/` is not used)
- React `18.3`
- TypeScript `5.5`
- Tailwind CSS
- `@supabase/ssr` for cookie-based session handling
- Vitest (unit) + Playwright (smoke)
- Deployed on Vercel (`apps/founder_portal/vercel.json`)

## Environment variables

| Name                            | Where it runs    | Required | Notes                                                    |
| ------------------------------- | ---------------- | -------- | -------------------------------------------------------- |
| `NEXT_PUBLIC_SUPABASE_URL`      | client + server  | yes      | Public Supabase project URL.                             |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | client + server  | yes      | Public anon key — RLS protects data in the database.     |
| `BACKEND_API_URL`               | server only      | yes      | FastAPI base URL (e.g. `https://api.coherence-fund...`). |

`SUPABASE_SERVICE_ROLE_KEY` **must never** be added to this project's
environment, on Vercel or locally — service-role operations live on the
backend. The Vercel project is also set up to fail fast if any
client-side env var does not start with `NEXT_PUBLIC_`.

Copy `.env.example` to `.env.local` for local development.

## Local development

```bash
cd apps/founder_portal
pnpm install
pnpm dev          # http://localhost:3001
pnpm lint
pnpm tsc --noEmit
pnpm build
pnpm vitest run
pnpm test:e2e     # requires Playwright browsers (`pnpm exec playwright install --with-deps chromium`)
```

## Regenerating the SDK

The committed `src/sdk/index.ts` is a hand-trimmed stub of the schemas the
portal actually consumes; it lets the project type-check and build before any
code generation has run. To produce the full client:

```bash
pnpm --filter @coherence/founder-portal generate:sdk
# or directly:
python3 ../../scripts/generate_ts_sdk.py
```

This invokes `openapi-typescript-codegen` (lazy-installed via `npx --yes`)
against `docs/specs/openapi_v1.yaml` and writes the result into
`src/sdk/`.

## Vercel deployment

`apps/founder_portal/vercel.json` configures `framework=nextjs` for the
founder portal project. The repo-level `vercel.json` is reserved for the
FastAPI fund API project. The infrastructure setup PDF in
`docs/specs/INFRASTRUCTURE_SETUP.md` (when published) covers project
creation, environment-variable wiring through Supabase + the secret
manager, and the `staging` / `production` Vercel project split. Until then,
the manual sequence is:

1. Create a Vercel project; root directory `apps/founder_portal`.
2. Wire the three env vars above (Production + Preview).
3. Push a branch — the `founder_portal` GitHub Actions workflow gates lint /
   typecheck / build / vitest / Playwright before the Vercel deploy.

## Testing notes

- **Vitest** mocks `fetch` directly; no Supabase or backend access in unit
  tests.
- **Playwright** intercepts `**/auth/v1/authorize**` so the smoke test never
  hits a real Supabase project. Tests run against `pnpm dev` on port 3001.
- The CI workflow injects stub env values for all three variables so
  `next build` can render server components without contacting Supabase.
