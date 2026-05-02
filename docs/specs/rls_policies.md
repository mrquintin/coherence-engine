# RLS (Row-Level Security) policies — fund backend

Status: implemented. The Python registry and Alembic migration install
founder-scoped RLS after the `fund_founders.founder_user_id` identity column
exists, so the policy DDL is valid on a fresh Postgres database.

## Why RLS

Supabase exposes a Postgres database directly to clients via PostgREST.
Without row-level security, any client holding an `anon` or `authenticated`
JWT can `SELECT * FROM fund_applications`. RLS is the **only** boundary
between a logged-in founder and another founder's application data — auth
checks in the FastAPI layer do nothing for traffic that bypasses FastAPI.

This applies even though our primary surface is the FastAPI service. Two
reasons:

1. The Supabase pooler URL is reachable from any service holding the DB
   credentials; defense-in-depth means a leaked pooler password does not
   yield the entire dataset.
2. Future read-paths (Realtime subscriptions, direct PostgREST reads from
   the founder dashboard) will not go through FastAPI at all.

## What is declared

`server/fund/security/rls.py` exports:

- `RLSPolicy` — frozen dataclass: `table`, `name`, `command`, `using_clause`,
  `with_check_clause`, `roles`.
- `RLS_POLICIES: list[RLSPolicy]` — the declarative registry.
- `apply_rls_policies(connection, policies=RLS_POLICIES)` — emits
  `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and idempotent
  `DROP POLICY IF EXISTS` + `CREATE POLICY` against a Postgres connection;
  no-op on SQLite.
- `revert_rls_policies(...)` — drops the policies and disables RLS.
- `render_policy_ddl(...)` — renders the full upgrade DDL block as a string
  (consumed by the snapshot test).

### Protected tables (initial set)

| Table                       | `authenticated` access                       | `service_role` |
| --------------------------- | -------------------------------------------- | -------------- |
| `fund_applications`         | SELECT via owning founder row                | ALL            |
| `fund_decisions`            | SELECT via owning application (subquery)     | ALL            |
| `fund_event_outbox`         | none                                         | ALL            |
| `fund_idempotency_records`  | none                                         | ALL            |
| `fund_argument_artifacts`   | SELECT via owning application (subquery)     | ALL            |

Default-deny: enabling RLS on a table without any matching policy denies all
access by default. We rely on this — for example, `fund_event_outbox` is
intentionally only accessible to `service_role`.

## How it is installed

Migration `20260425_000001_rls_policies.py`:

```sql
-- Postgres only; SQLite migrations no-op.
ALTER TABLE public."fund_applications" ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "fund_applications_founder_select"
    ON public."fund_applications";
CREATE POLICY "fund_applications_founder_select" ON public."fund_applications"
    FOR SELECT TO authenticated
    USING (
      EXISTS (
        SELECT 1
        FROM public.fund_founders f
        WHERE f.id = founder_id
          AND f.founder_user_id = auth.uid()::text
      )
    );
-- ... etc.
```

`apply_rls_policies` issues `DROP POLICY IF EXISTS` before each `CREATE
POLICY`, so reruns of the migration converge to the declared state without
operator intervention.

## Why SQLite is a no-op

SQLite has no row-level security mechanism. The default-dev experience must
remain `python -m pytest` against a local SQLite file with no DB setup. The
applier checks `connection.dialect.name`; on SQLite (or anything other than
`postgresql`) it returns silently.

This means RLS coverage is enforced **only on Postgres**. Tests that need to
verify RLS behavior (e.g. that founder A cannot read founder B's
applications) must run against a real Postgres instance — see prompt 25 for
the founder-scoped integration tests.

## Drift detection

`tests/test_rls_policies.py::test_rendered_ddl_snapshot_matches_pinned`
pins the rendered DDL as a snapshot string. Any change to `RLS_POLICIES`
forces a corresponding update to the snapshot, so silent drift between the
registry and what migrations emit is impossible.

## Pending follow-ups

- Add RLS integration tests against a real Postgres/Supabase instance that
  switches into the `authenticated` role and verifies cross-founder denial.
- Add policies for any new founder-visible tables introduced after this
  scaffold.
