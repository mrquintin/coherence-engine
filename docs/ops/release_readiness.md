# Release readiness checklist (runbook)

This runbook is the operator-facing companion to
[`deploy/scripts/release_readiness_check.py`](../../deploy/scripts/release_readiness_check.py).

The script consolidates the post-conditions that close out the automated
pre-seed pipeline (prompts 01-20). It is **deterministic**, **network-free**,
and runs entirely against files already on disk plus the in-tree `coherence_engine`
package. No production database, no governed dataset mutation, no outbound HTTP.

## Quick invocation

From the package root (`Coherence_Engine_Project/coherence_engine`):

```bash
# Direct invocation (prefers repo parent on PYTHONPATH).
PYTHONPATH=.. python3 deploy/scripts/release_readiness_check.py \
  --json-out /tmp/ce-readiness.json \
  --markdown-out /tmp/ce-readiness.md

# Or via the committed Make target (writes artifacts/release-readiness.{json,md}).
make release-readiness
```

The script prints the Markdown summary to stdout by default. Use `--quiet`
to suppress stdout (the JSON and Markdown files are still written when
their `--*-out` flag is passed).

## Exit codes

| Code | Meaning | Operator action |
|------|---------|-----------------|
| `0`  | Every check passed. Safe to proceed. | None. Record the JSON report as release evidence. |
| `1`  | At least one **soft** failure. The report lists each failing `check_id` + `reason_code`. | Remediate according to the failure mode table below, then re-run. |
| `2`  | **Fixture / loader error** - the script could not complete a check because a dependency blew up. | Fix the tooling issue (usually an import error, missing `alembic.ini`, or unreadable file) before re-running. Exit `2` means the script itself is not usable on this tree, not that product state is broken. |

## Report shape (`schema_version = release-readiness-report-v1`)

```json
{
  "schema_version": "release-readiness-report-v1",
  "exit_code": 0,
  "summary": {"total": 10, "passed": 10, "failed": 0, "errors": 0},
  "results": [
    {
      "check_id": "alembic_head",
      "status": "pass",
      "reason_code": null,
      "detail": "single alembic head resolved to 20260417_000006",
      "evidence": {"heads": ["20260417_000006"], "versions_files": ["..."]}
    }
  ]
}
```

Every check contributes exactly one row, in a stable declared order so
two back-to-back runs on an unchanged tree produce byte-identical JSON
(the script uses `json.dumps(..., sort_keys=True, indent=2)`).

## Checks and failure modes

### `alembic_head`

Uses `alembic.script.ScriptDirectory` to enumerate heads.

- `multiple_alembic_heads` - more than one head present. A migration was
  added on a branch; rebase / collapse the branch so `alembic heads`
  returns exactly one revision.
- `alembic_head_file_missing` - the revision reported by `alembic heads`
  has no matching file in `alembic/versions/`. Usually means a checkout
  is partial; `git status` will show the missing file.
- `alembic_ini_missing` / `alembic_import_failed` / `alembic_script_load_failed`
  - fixture / loader errors (exit `2`). Confirm `alembic` is installed and
  that you are running from the package root.

### `decision_policy_version`

Imports `DECISION_POLICY_VERSION` from
`server/fund/services/decision_policy.py` and compares it to the pinned
constant `"decision-policy-v1"`.

- `decision_policy_version_mismatch` - the policy version was bumped
  without updating this check (intentional) or was reverted (unintentional).
  Both outcomes are contract changes, so the fix is intentional: update
  `docs/specs/decision_policy_spec.md` and the expected string in this
  runbook and the check source.

### `event_schemas`

For every entry in `SUPPORTED_EVENTS` (`interview_completed`,
`argument_compiled`, `decision_issued`, `founder_notified`), loads the
schema file from `server/fund/schemas/events/` and calls
`validate_event(event, examples[0])`.

- `event_schema_example_invalid` - the committed `examples[]` entry no
  longer satisfies the schema. Either the schema drifted or the example
  was hand-edited. Regenerate or hand-fix the example in the schema
  file.
- `event_schema_load_failed` / `event_schemas_import_failed` -
  fixture errors (exit `2`).

### `prompt_registry`

Calls `prompt_registry.load_registry()` +
`prompt_registry.verify_registry()` against the shipped registry at
`data/prompts/registry.json`.

- `prompt_registry_verify_failed` - a prompt body's SHA-256 no longer
  matches the registry declaration. Run `python -m coherence_engine
  prompt-registry verify --json` locally, update the registry via the
  standard prompt-ops flow, and re-run.

### `e2e_integration_test`

Confirms `tests/integration/test_e2e_pipeline.py` exists and contains
`@pytest.mark.e2e`.

- `e2e_test_missing` - the file was moved or deleted. Restore it or
  update this check if the integration test was intentionally relocated
  (requires a new prompt / PR).
- `e2e_marker_missing` - the decorator was dropped. The default
  offline CI suite depends on the marker being present; re-add it.

### `backtest_spec`

Confirms `docs/specs/backtest_spec.md` is present. Any change here is
deliberate (prompt 11 ownership).

### `red_team_expected_matrix`

Confirms `tests/adversarial/labels.json` is present, parseable, and that
every entry carries an `expected_verdict` key (mirrors the hygiene
assertion in `tests/test_red_team_harness.py`).

- `red_team_matrix_malformed` - operator-readable JSON says which
  entries are missing the expected field. Restore or rewrite.

### `admin_dashboard_router`

Imports `server/fund/routers/admin_ui.py` and verifies:

1. `router.prefix == "/admin"`.
2. `server/fund/app.py` calls `include_router(admin_ui_router)` in
   `create_app()`.

- `admin_router_prefix_wrong` - the router prefix was edited. The dashboard
  expects `/admin` for static asset paths and HTMX attributes.
- `admin_router_not_mounted` - the router is defined but not wired into
  `create_app()`; the admin UI will return 404 at runtime.

### `status_doc_prompt_recap` / `continuation_doc_prompt_recap`

Scans `COHERENCE_ENGINE_PROJECT_STATUS.txt` and
`COHERENCE_ENGINE_CONTINUATION_PROMPT.txt` for references to prompts
`01`-`20` (via the regex `r"prompt\s+0?(\d{1,2})\b"` in the check
source).

- `docs_prompt_recap_incomplete` - the `missing` list in the evidence
  block shows which prompt numbers are not referenced. Add a recap line
  for each (see the "Automated pre-seed pipeline completion" block at
  the top of each doc).

## Recommended CI invocation

```yaml
- name: Release readiness
  run: |
    cd Coherence_Engine_Project/coherence_engine
    make release-readiness
  # Upload artifacts/release-readiness.json + artifacts/release-readiness.md
  # on both success and failure for audit evidence.
```

The JSON report is stable across runs on an unchanged tree, so
`diff` against the previous release artifact is a useful second-line
drift signal.

## Adding a new check

1. Add a `_check_<name>()` function in
   `deploy/scripts/release_readiness_check.py`. Return a single
   `CheckResult`. Follow the existing error / soft-fail / pass
   distinction (error => exit `2`).
2. Append `("<check_id>", _check_<name>)` to the `CHECKS` tuple (order
   is stable on purpose - new entries go at the end unless you are
   deliberately resequencing the report).
3. Add a row in the "Checks and failure modes" table above documenting
   each `reason_code` the check can produce.
4. Extend `tests/test_release_readiness_check.py` with a positive +
   negative case for the new check (see existing tests for the pattern:
   temporarily remove / mutate a required file and assert the specific
   `reason_code` appears).

The check tuple is intentionally the single source of truth for
ordering: the JSON and Markdown outputs iterate `CHECKS`, so the report
layout is coupled to the registration order.
