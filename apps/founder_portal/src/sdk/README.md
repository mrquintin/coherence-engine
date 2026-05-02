# Generated TypeScript SDK

This directory is regenerated from `docs/specs/openapi_v1.yaml` by
`scripts/generate_ts_sdk.py`. The current `index.ts` is a hand-trimmed stub
that exposes only the schemas the founder portal actually imports — kept
under version control so the project type-checks and builds before the
codegen has been run.

To refresh the full client:

```
pnpm --filter @coherence/founder-portal generate:sdk
```

The generator (`openapi-typescript-codegen`) will replace this directory in
place. Do not edit the generated files by hand — update the OpenAPI spec
and re-run.
