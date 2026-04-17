# Prompt Registry Specification (v1)

**Status:** prod
**Schema version:** `prompt-registry-v1`
**Prompt registry file:** `data/prompts/registry.json`
**Bodies directory:** `data/prompts/bodies/`

The prompt registry is the single source of truth for every LLM prompt the
Coherence Engine uses in production. It pins each prompt to a specific
SHA-256 content hash so that the decision artifact can prove, after the fact,
which exact prompt text was active when a decision was issued.

## Why a registry

1. **Auditability.** Regulators and LPs can verify that the diligence process
   used prompt content under version control, not an ad-hoc string.
2. **Reproducibility.** `decision_artifact.v1` pins the registry's digest in
   its `pins.prompt_registry_digest` field. Replaying a decision with a
   different prompt set will produce a different artifact.
3. **Safe evolution.** Drafts and shadow variants can live alongside prod
   prompts without being accidentally wired into the hot path.

## Registry schema

```json
{
  "schema_version": "prompt-registry-v1",
  "prompts": [
    {
      "id": "interview_opening",
      "version": "1.0.0",
      "status": "prod",
      "body_path": "data/prompts/bodies/interview_opening.v1.md",
      "content_sha256": "<hex sha256 of the body file bytes>",
      "owner": "fund-ops"
    }
  ]
}
```

### Field semantics

| Field              | Type   | Rules                                                                                                     |
| ------------------ | ------ | --------------------------------------------------------------------------------------------------------- |
| `id`               | string | Stable identifier; unique together with `version`.                                                        |
| `version`          | string | Semantic version (`MAJOR.MINOR.PATCH`). Bump `MAJOR` on breaking prompt changes.                          |
| `status`           | enum   | One of `draft`, `shadow`, `prod`.                                                                         |
| `body_path`        | string | Repo-relative path to the Markdown body file.                                                             |
| `content_sha256`   | string | Lowercase hex SHA-256 of **raw on-disk bytes** (no normalization).                                        |
| `owner`            | string | Team or individual responsible for changes (e.g. `fund-ops`).                                             |

Rules enforced by `load_registry`:

- `schema_version` must equal `prompt-registry-v1`.
- Every prompt entry must contain all six required fields.
- `status` must be one of `draft`, `shadow`, `prod`.
- `(id, version)` pairs must be unique.
- Prompt bodies must live on disk; they are never embedded inline in the
  registry JSON.

## Digest semantics

`registry_digest(registry)` returns a SHA-256 over the JSON-encoded list of
sorted `(id, version, content_sha256)` tuples. It is:

- **Order-insensitive** with respect to the entries' declaration order in the
  JSON file (entries are sorted before hashing).
- **Content-sensitive**: any change to a body file's SHA changes the digest.
- **Version-sensitive**: bumping a prompt's version changes the digest.

The digest is embedded in the decision artifact's `pins.prompt_registry_digest`
field so auditors can detect prompt drift between decision runs.

## CLI

`python -m coherence_engine prompt-registry {list | verify | digest}`

| Verb    | Purpose                                                           | Exit code on failure |
| ------- | ----------------------------------------------------------------- | -------------------- |
| `list`  | Print every registered prompt with id/version/status/sha prefix.  | 2 (load error)       |
| `verify`| Recompute each body's SHA-256 and compare against the registry.   | 2 (mismatch/missing) |
| `digest`| Print the registry digest (used for artifact pinning).            | 2 (load error)       |

All three verbs accept `--registry <path>` to operate on a non-default
registry file (useful for tests, drafts, and environment-specific overrides).
`list` and `verify` additionally accept `--json` for machine-readable output.

Exit code `2` matches the parity convention set by
`validate-historical-export`: zero on success, two on policy/verification
failure, one on unexpected errors.

## Python API

`coherence_engine.server.fund.services.prompt_registry` exposes:

- `load_registry(path: Path | None = None) -> Registry` — parse and validate.
- `verify_registry(registry, repo_root=None) -> VerificationReport` —
  recompute hashes; returns `{ok, mismatches, missing}`.
- `registry_digest(registry) -> str` — stable sha256 over
  sorted `(id, version, content_sha256)` tuples.
- `resolve(prompt_id, version, registry=None) -> PromptEntry` — lookup.

Dataclasses: `PromptEntry`, `Registry`, `Mismatch`, `VerificationReport`.

## Lifecycle

1. **Draft**: author writes a new body file under `data/prompts/bodies/` and
   adds a registry entry with `status: "draft"`.
2. **Shadow**: flipped to `status: "shadow"` to be exercised in parallel with
   prod traffic without affecting decisions. The artifact still pins the
   whole registry digest, so shadow prompts are part of the audit trail.
3. **Prod**: promoted to `status: "prod"`. Any change to its body bytes
   requires a new `version` — the registry digest will change and
   downstream decision artifacts will reflect the new pin automatically.

## Invariants

- Body SHA-256 is always computed over raw bytes (`hashlib.sha256(data).hexdigest()`).
- No normalization, stripping, or re-encoding is performed before hashing.
- The registry JSON never contains prompt body text.
- The shipped registry at `data/prompts/registry.json` must always verify OK
  in CI (see `tests/test_prompt_registry.py::test_shipped_registry_verifies_against_body_files`).
