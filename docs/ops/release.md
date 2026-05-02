# Release pipeline (operator runbook)

This runbook documents the **tag-triggered** release pipeline added in
prompt 67. It is the companion to
[`.github/workflows/release.yml`](../../.github/workflows/release.yml),
[`scripts/release/cut_release.py`](../../scripts/release/cut_release.py),
and [`scripts/release/changelog.py`](../../scripts/release/changelog.py).

## TL;DR

```bash
# 1. Preview the next release locally (no writes, no network calls).
python scripts/release/cut_release.py --dry-run --since v0.1.7

# 2. Commit your work using Conventional Commits, then cut the tag.
git tag -s v0.1.8 -m "v0.1.8"
git push origin v0.1.8
```

The push of a `v*.*.*` tag fires
[`.github/workflows/release.yml`](../../.github/workflows/release.yml),
which runs the full release sequence below. The previously-existing
`workflow_dispatch` deploy path (preflight + production rollout) is
unchanged and remains gated by `github.event_name == 'workflow_dispatch'`.

## Semver policy

`scripts/release/cut_release.py` infers the next version from the
commits between the previous tag and `HEAD`:

| Commit signal                                 | Bump   |
|-----------------------------------------------|--------|
| `BREAKING CHANGE:` body or `type!:` header    | major  |
| Any `feat(...)` commit                        | minor  |
| Otherwise (`fix`, `chore`, `docs`, `ci`, …)   | patch  |

Override with `--bump major|minor|patch`. The `VERSION` file is the
canonical source of truth.

## Conventional Commits

Allowed types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`,
`build`, `ci`, `chore`, `revert`. Format:

```
type(scope)?: subject
type(scope)!: subject       # breaking
```

Headers that don't match the pattern are not lost — they fall through
into the `Chores` section so the changelog is always complete.

## What the workflow does on a tag push

1. **`release_lint_test`** — ruff + mypy strict + full pytest. A failure
   here aborts the release before any artifact is produced.
2. **`release_readiness_gate`** — runs
   [`deploy/scripts/release_readiness_check.py`](../../deploy/scripts/release_readiness_check.py).
   The script's contract is that exit 0 ⇔ every committed checklist item
   reports `status == "pass"`. **A non-zero exit aborts the release**;
   no images, charts, or release entries are published.
3. **`release_changelog`** — runs `cut_release.py --require-readiness`
   and emits `artifacts/CHANGELOG_RELEASE.md` (release-scoped, fed to
   `gh release create --notes-file`) plus `artifacts/release-plan.json`
   (machine-readable bump summary).
4. **`release_build_sign`** — matrix job over `backend`,
   `founder-portal`, `partner-dashboard`. Each:
   * `docker buildx build --push` to `ghcr.io/<org>/<component>:<version>`,
   * `cosign sign` keyless via Sigstore + GitHub OIDC,
   * `cosign attest --type slsaprovenance` of the build provenance.
5. **`release_helm_chart`** — `helm lint` + `helm template` (smoke
   render), `helm package` with the resolved version, OCI push to
   `oci://ghcr.io/<org>/charts/coherence-fund:<version>`, then
   `cosign sign` of the chart digest plus a `cosign sign-blob` of the
   `.tgz` for offline verification.
6. **`release_publish`** — builds the Python wheel + sdist,
   `gh release create v<version> --notes-file CHANGELOG_RELEASE.md`,
   uploads `*.whl`, `*.tar.gz`, `coherence-fund-<version>.tgz`, and the
   detached `*.sig` blobs. Finally opens
   `release/post-<version>-version-bump` against `main` with a single
   commit bumping `VERSION` to the next patch.

## Verifying a published release

Container image:

```bash
cosign verify \
  --certificate-identity-regexp "^https://github\.com/Michael-Quintin/coherence-engine/.+" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/Michael-Quintin/coherence-engine/backend:0.1.8
```

Helm chart `.tgz` (offline / detached):

```bash
cosign verify-blob \
  --signature coherence-fund-0.1.8.tgz.sig \
  --certificate coherence-fund-0.1.8.tgz.pem \
  --certificate-identity-regexp "^https://github\.com/Michael-Quintin/coherence-engine/.+" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  coherence-fund-0.1.8.tgz
```

SLSA provenance attestation (image):

```bash
cosign verify-attestation \
  --type slsaprovenance \
  --certificate-identity-regexp "^https://github\.com/Michael-Quintin/coherence-engine/.+" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/Michael-Quintin/coherence-engine/backend:0.1.8
```

## Local helm rendering

The umbrella chart at `deploy/helm/` aggregates the existing
`coherence-fund` subchart. The `charts/coherence-fund` entry is a
symlink to the in-tree subchart so you can render in place:

```bash
helm template deploy/helm
helm lint   deploy/helm/coherence-fund
```

For environment-specific overlays, continue using the existing
`deploy/helm/coherence-fund/values-prod*.yaml` files; pass them to
`helm upgrade --install` directly against the subchart, not the
umbrella.

## Failure-mode guardrails

| Prohibition                                     | How it's enforced |
|--------------------------------------------------|-------------------|
| Never publish if readiness fails                 | `release_readiness_gate` is `needs:` for every downstream job; `cut_release.py --require-readiness` re-checks before changelog write. |
| Never push an unsigned image                     | Each `release_build_sign` matrix entry runs `cosign sign --yes <image>@<digest>` immediately after `docker push`, before any downstream job consumes the image. |
| Never skip the post-release VERSION bump PR     | `release_publish` runs `gh pr create --base main` with a `release/post-<version>-version-bump` branch as the final step; the PR title and body are deterministic. |

## Manual hot-fix flow

If you need to publish a hotfix without a tag (e.g. an out-of-band
patch), don't bypass the workflow — push a tag against the hotfix
commit. The release_readiness_gate will re-run against that exact
revision; this is the only authorized entry point for a signed
release.

## Cosign + Sigstore: model and trust roots

Signing is **keyless**: cosign uses GitHub Actions' OIDC token to
request a short-lived certificate from the Fulcio CA, signs, and
records the signature in the public Rekor transparency log. There is
no long-lived private key to rotate. Verification asserts that the
signing identity is a workflow under this repository (the
`certificate-identity-regexp` shown above).
