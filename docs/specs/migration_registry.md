# Migration Registry — Postgres ↔ SQLite parity ledger

**Owner:** Coherence Engine fund-backend / DB-ops
**Scope:** every Alembic revision under `alembic/versions/`
**Schema version:** `migration-registry-v1`
**Source of truth:** `data/governed/migration_registry.json`
**Auditor:** `deploy/scripts/audit_migrations_postgres_parity.py`

The migration registry is a deterministic ledger of each Alembic
revision's compatibility with the two dialects we run against:

- **PostgreSQL** — hosted environments (staging, prod, ops backups).
- **SQLite** — local dev DB and the test suite's throwaway engine.

The registry is produced by the auditor, not by humans, so every
edit is reproducible from the migration files + the latest commit's
authored timestamp.

This prompt (21) creates the audit + registry only — the rewrite of
defective migrations lands in prompt 24, which will consume this
registry.

## Schema (`migration-registry-v1`)

```json
{
  "schema_version": "migration-registry-v1",
  "audited_at": "<ISO 8601 with timezone, from `git log -1 --format=%aI`>",
  "revisions": [
    {
      "revision": "20260417_000002",
      "file": "alembic/versions/20260417_000002_artifact_kind.py",
      "postgres_compatible": true,
      "sqlite_compatible": true,
      "warnings": [
        {"code": "...", "message": "...", "lineno": 85}
      ],
      "errors": []
    }
  ]
}
```

Field rules:

- `audited_at` MUST come from `git log -1 --format=%aI`. Wall-clock
  time is forbidden so the registry is byte-stable in CI.
- `revisions` is sorted by the on-disk filename (which is the same
  order Alembic walks the version tree).
- `errors` and `warnings` are sorted by `(lineno, code)`.

## Audit rules

| Code                                                  | Severity | Trigger                                                                                          | Effect                          |
|-------------------------------------------------------|----------|--------------------------------------------------------------------------------------------------|----------------------------------|
| `alter_column_server_default_none_sqlite`             | error    | `op.alter_column(..., server_default=None)`                                                      | `sqlite_compatible = false`     |
| `op_execute_pragma_sqlite_only`                       | error    | `op.execute("...PRAGMA...")`                                                                      | `postgres_compatible = false`   |
| `op_execute_if_not_exists_outside_create_table`       | error    | `op.execute("... IF NOT EXISTS ...")` without `CREATE TABLE` in the same statement               | `postgres_compatible = false`   |
| `boolean_no_server_default`                           | warning  | `sa.Column(..., sa.Boolean(), nullable=False)` with no `server_default=`                          | informational                   |
| `sqlite_only_pattern_batch_alter_table`               | warning  | `op.batch_alter_table(...)`                                                                       | informational (NOT a defect)    |
| `fk_addition_no_not_valid`                            | warning  | `op.create_foreign_key(...)` without a follow-up `ALTER TABLE ... NOT VALID`                      | informational                   |

Severity contract:

- Any non-empty `errors` list **must** be addressed before the
  revision can ship to a Postgres CI environment. CI uses
  `python -m coherence_engine db audit-migrations` (without
  `--write-registry`) and treats exit code 2 as "block the merge".
- `warnings` never block CI but feed prompt 24's rewrite triage.

## CLI

```
python -m coherence_engine db audit-migrations [--write-registry]
                                               [--versions-dir PATH]
                                               [--registry-path PATH]
                                               [--json]
                                               [--audited-at ISO]
```

- Without `--write-registry`: prints the audit table (or the JSON if
  `--json` is set) and exits 0 if every revision has
  `errors == []`, otherwise exit 2.
- With `--write-registry`: also overwrites
  `data/governed/migration_registry.json`.

## Convention

- Any revision that may run against Postgres in CI MUST have
  `errors == []` in the registry. Warnings are advisory.
- `audited_at` is always the latest commit's authored ISO
  timestamp, so a stale registry is detectable by re-running
  the auditor and checking the diff.
- The registry is a generated artifact. Hand-edits are not
  supported — re-run the CLI with `--write-registry`.
