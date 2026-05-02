# Coherence Engine — public site

Astro-static marketing + research site. Deploys to Vercel as a separate
project from `apps/founder_portal/` and `apps/partner_dashboard/`.

## Pages

- `/` — thesis
- `/research` — paper index (RSS at `/research/rss.xml`)
- `/research/[slug]` — MDX papers with KaTeX math
- `/fund` — public-facing thesis & process (no LP-restricted material)
- `/results` — live validation report stub (built by prompt 46)
- `/contact` — research, founder, and press contacts

## Local dev

```bash
pnpm install
pnpm dev          # http://localhost:4321
pnpm build        # → dist/
pnpm preview      # serve dist/
pnpm test         # build + caveat checks
RUN_LIGHTHOUSE=1 pnpm test    # adds Lighthouse perf budget
```

## Deploy

The Vercel project root should be `apps/site/`. The `apps/site/vercel.json`
in this directory configures the build. The repo-root `vercel.json` is owned
by the FastAPI fund API; do not point this site at it.

## Constraints

- No backend calls at build time. Everything is static.
- Every research page renders the predictive-validity caveat (verbatim
  banner from `src/components/CaveatBanner.astro`).
- No LP-restricted documents on this site.

## Known local-only build issue

If your absolute project path contains an apostrophe (e.g.
`/Users/.../Michael's MacBook Pro/...`), Vite's `import.meta.glob`
resolver fails to enumerate `src/content/research/*.mdx` and
`getCollection('research')` returns empty. CI and Vercel deploy paths
do not contain apostrophes, so production builds are unaffected.

Local workaround:

```bash
cp -R apps/site /tmp/coherence-site
cd /tmp/coherence-site && pnpm install && pnpm build
```

Tracked upstream as a Vite/fast-glob escaping issue with single quotes in
absolute paths. Do not "fix" by switching collection types — this is
purely environmental.
