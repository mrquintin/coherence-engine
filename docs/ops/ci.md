# CI workflow guide

Wave 17, prompt 65 of 70 introduced a consolidated CI matrix at
`.github/workflows/ci.yml`. This document is the operator runbook for
that workflow and the surrounding GitHub Actions configuration
(CodeQL, Dependabot, release_readiness, deploy preview).

## At a glance

`ci.yml` runs a single matrix with parallel jobs gated by path filters:

| Job                  | Trigger path filter | Notes                                              |
|----------------------|---------------------|----------------------------------------------------|
| `changes`            | always              | Emits `server` / `apps` / per-app booleans         |
| `lint`               | server or apps      | ruff (backend) + eslint (each Next.js app)         |
| `type`               | server or apps      | mypy --strict (backend) + tsc --noEmit (frontends) |
| `unit_backend`       | server              | pytest, fast (no integration/e2e/cloud markers)    |
| `integration_backend`| server              | pytest -m integration; Postgres + Redis services   |
| `unit_frontend`      | apps                | vitest in each Next.js app (matrix)                |
| `e2e_founder_portal` | apps/founder_portal | Playwright smoke                                   |
| `build_backend`      | server              | wheel + Docker image (push to GHCR on main)        |
| `build_apps`         | apps                | next/astro build + artifact (matrix per app)       |
| `deploy_preview`     | apps (PR only)      | Vercel preview URL per app                         |
| `deploy_staging`     | main push           | Gated by the `staging` GitHub Environment          |
| `release_readiness`  | always (post-unit)  | `deploy/scripts/release_readiness_check.py`        |

The `paths:` filter on the workflow itself plus the `dorny/paths-filter`
job mean a frontend-only PR does **not** start the backend integration
suite, and vice versa.

## Required checks for `main`

Configure GitHub branch protection on `main` to require these checks
to pass before merge:

- `lint`
- `type`
- `unit_backend`
- `build_backend`
- `release_readiness`

`integration_backend`, `e2e_founder_portal`, and the deploy jobs are
**not** required — they are advisory and can be flaky against ephemeral
service containers. Promote them to required only after a soak window.

## Caches

| Cache                  | Keyed on                                          |
|------------------------|---------------------------------------------------|
| pip                    | Python version + lockfile (via `actions/setup-python`) |
| pnpm store             | per-app `package.json` hash                       |
| Playwright browsers    | per-app `package.json` hash                       |
| Docker buildx (GHA)    | `cache-from: type=gha`, `cache-to: type=gha,mode=max` |

## Local mirror

```sh
make ci-local
```

runs the required-checks subset (ruff, mypy --strict, fast pytest, and
`release_readiness_check.py`). It produces
`artifacts/release-readiness.{json,md}` for inspection. It does **not**
run integration tests, Playwright, or Docker builds — those need
service containers and Buildx and live in CI.

## CodeQL

`.github/codeql.yml` configures CodeQL for `python` and `javascript`,
scoped to source paths and excluding fixtures, vendored bundles, and
build outputs. Wire it into the required-checks set after the first
clean baseline run.

## Dependabot

`.github/dependabot.yml` opens grouped weekly PRs for:

- `pip` (root pyproject.toml + requirements*.txt)
- `npm` (each app under `apps/`)
- `github-actions` (workflow YAML)

Updates are grouped per ecosystem so the queue gets one PR per
ecosystem per week instead of dozens.

## release_readiness

The `release_readiness` job in `ci.yml` mirrors `make release-readiness`
and uploads `artifacts/release-readiness.{json,md}` as a workflow
artifact. Exit codes:

- `0` — all checks pass
- `1` — at least one **soft** failure (per-check `reason_code` recorded)
- `2` — fixture / loader error (script not usable)

The CI step uses `|| true` so the workflow does not abort on a soft
failure; review the uploaded `release-readiness.md` instead. See
`docs/ops/release_readiness.md` for per-check failure-mode guidance.

## Deploy preview

`deploy_preview` calls `vercel pull && vercel build && vercel deploy`
per app on every PR that touches `apps/**`. The job soft-skips when
`VERCEL_TOKEN` is not configured (forks, secret-less runs). Per-app
project IDs are read from per-app secrets named
`VERCEL_PROJECT_ID_<app>` (e.g. `VERCEL_PROJECT_ID_founder_portal`).

## Deploy staging

`deploy_staging` runs only on `push` to `main` and is gated by the
`staging` GitHub Environment. Configure required reviewers and branch
policy on the environment in repo settings — the job will block until
sign-off. The job posts to `STAGING_DEPLOY_HOOK` (a downstream
ArgoCD / Helm runner / Vercel deploy webhook) and soft-skips when the
secret is unset.

## Prohibitions

- Do **not** run paid integrations (Stripe live, etc.) from `ci.yml`.
  Sandbox keys live behind a separate workflow gated by an environment.
- Do **not** cache secrets in build artifacts.
- Do **not** delete `apps/**` per-app workflows without porting their
  tests into `ci.yml` first — the per-app workflows preserve
  directory-scoped checks for app-only changes.
