# Partner Dashboard

Next.js 14 (App Router) operator dashboard for Coherence Fund partners.
Mirrors the founder portal scaffolding (`apps/founder_portal/`) and consumes the
same `/api/v1/partner/*` namespace exposed by the FastAPI backend.

## RBAC

The dashboard requires the Supabase JWT to carry `app_metadata.role` of either
`partner` or `admin`. Backend enforcement lives in
`server/fund/services/decision_overrides.py::require_role` and is applied to
every route in `server/fund/routers/partner_api.py`.

## Local development

```bash
pnpm install
pnpm dev          # Next.js on :3002
pnpm typecheck
pnpm test         # vitest
pnpm test:e2e     # playwright
```

## Pages

- `/pipeline` — pivot table of in-flight applications (filters: domain,
  verdict, mode); cursor-paginated; URL params drive filters so refresh and
  share-links work.
- `/applications/[id]` — full decision artifact viewer, override summary,
  link to the override page.
- `/applications/[id]/override` — manual-review override form. Requires
  `reason_code` + `reason_text` (≥40 chars). Memo URL required for
  pass→reject overrides.
- `/audit` — read-only audit-log feed.

## Fallback

The legacy HTMX admin dashboard at `/admin` (prompt 19) is preserved as a
fallback. If the Next.js dashboard is unavailable, partners with the `admin`
role can use that surface for read-only triage.
