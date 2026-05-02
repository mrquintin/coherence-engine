"""Tests for the RLS policy registry and applier.

Declarative-only — no real Postgres connection. The tests assert:

* Every entry in ``RLS_POLICIES`` is a valid :class:`RLSPolicy`.
* The protected-table list matches the surface declared in prompt 22.
* ``apply_rls_policies`` is a no-op against an in-memory SQLite engine
  (does not raise, does not emit DDL).
* The rendered Postgres DDL matches a pinned snapshot — drift between
  this test and ``rls.py`` has to be acknowledged by updating the
  snapshot.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from coherence_engine.server.fund.security.rls import (
    RLS_POLICIES,
    RLSPolicy,
    apply_rls_policies,
    render_policy_ddl,
    revert_rls_policies,
    rls_tables,
)


_EXPECTED_TABLES = [
    "fund_applications",
    "fund_decisions",
    "fund_event_outbox",
    "fund_idempotency_records",
    "fund_argument_artifacts",
]


def test_registry_is_non_empty_and_well_typed():
    assert len(RLS_POLICIES) > 0
    for policy in RLS_POLICIES:
        assert isinstance(policy, RLSPolicy)
        assert policy.table
        assert policy.name
        assert policy.command in {"SELECT", "INSERT", "UPDATE", "DELETE", "ALL"}
        assert policy.roles, "policy must declare at least one role"


def test_registry_covers_expected_tables():
    assert rls_tables() == _EXPECTED_TABLES


def test_each_protected_table_has_service_role_full_access():
    for table in _EXPECTED_TABLES:
        matches = [
            p
            for p in RLS_POLICIES
            if p.table == table
            and p.command == "ALL"
            and "service_role" in p.roles
        ]
        assert matches, f"{table} missing service_role ALL policy"


def test_invalid_command_rejected():
    with pytest.raises(ValueError):
        RLSPolicy(
            table="t",
            name="bogus",
            command="TRUNCATE",
            using_clause="true",
            roles=("service_role",),
        )


def test_empty_roles_rejected():
    with pytest.raises(ValueError):
        RLSPolicy(
            table="t",
            name="bogus",
            command="SELECT",
            using_clause="true",
            roles=(),
        )


def test_insert_without_with_check_rejected():
    with pytest.raises(ValueError):
        RLSPolicy(
            table="t",
            name="bad_insert",
            command="INSERT",
            roles=("service_role",),
        )


def test_render_drop_is_idempotent_form():
    p = RLS_POLICIES[0]
    assert "DROP POLICY IF EXISTS" in p.render_drop()
    assert p.table in p.render_drop()
    assert p.name in p.render_drop()


def test_render_create_includes_role_and_command():
    p = RLS_POLICIES[0]
    ddl = p.render_create()
    assert "CREATE POLICY" in ddl
    assert f'"{p.name}"' in ddl
    assert p.table in ddl
    assert f"FOR {p.command}" in ddl
    for role in p.roles:
        assert role in ddl


def test_apply_rls_policies_sqlite_is_noop():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        # Must not raise; SQLite has no RLS support.
        apply_rls_policies(conn, RLS_POLICIES)
        revert_rls_policies(conn, RLS_POLICIES)


def test_rendered_ddl_snapshot_matches_pinned():
    """Snapshot test: updating the registry must update this string.

    The snapshot is intentionally exhaustive — it is the contract the
    Alembic migration consumes. If you change RLS_POLICIES, regenerate
    this string by running ``render_policy_ddl()`` and pasting the
    result here.
    """
    rendered = render_policy_ddl()
    assert rendered == _EXPECTED_DDL


_EXPECTED_DDL = """\
ALTER TABLE public."fund_applications" ENABLE ROW LEVEL SECURITY;
ALTER TABLE public."fund_decisions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE public."fund_event_outbox" ENABLE ROW LEVEL SECURITY;
ALTER TABLE public."fund_idempotency_records" ENABLE ROW LEVEL SECURITY;
ALTER TABLE public."fund_argument_artifacts" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "fund_applications_founder_select" ON public."fund_applications";
DROP POLICY IF EXISTS "fund_applications_service_all" ON public."fund_applications";
DROP POLICY IF EXISTS "fund_decisions_founder_select" ON public."fund_decisions";
DROP POLICY IF EXISTS "fund_decisions_service_all" ON public."fund_decisions";
DROP POLICY IF EXISTS "fund_event_outbox_service_all" ON public."fund_event_outbox";
DROP POLICY IF EXISTS "fund_idempotency_records_service_all" ON public."fund_idempotency_records";
DROP POLICY IF EXISTS "fund_argument_artifacts_founder_select" ON public."fund_argument_artifacts";
DROP POLICY IF EXISTS "fund_argument_artifacts_service_all" ON public."fund_argument_artifacts";

CREATE POLICY "fund_applications_founder_select" ON public."fund_applications"
    FOR SELECT
    TO authenticated
    USING (EXISTS (SELECT 1 FROM public.fund_founders f WHERE f.id = founder_id AND f.founder_user_id = auth.uid()::text));
CREATE POLICY "fund_applications_service_all" ON public."fund_applications"
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
CREATE POLICY "fund_decisions_founder_select" ON public."fund_decisions"
    FOR SELECT
    TO authenticated
    USING (EXISTS (SELECT 1 FROM public.fund_applications a JOIN public.fund_founders f ON f.id = a.founder_id WHERE a.id = application_id AND f.founder_user_id = auth.uid()::text));
CREATE POLICY "fund_decisions_service_all" ON public."fund_decisions"
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
CREATE POLICY "fund_event_outbox_service_all" ON public."fund_event_outbox"
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
CREATE POLICY "fund_idempotency_records_service_all" ON public."fund_idempotency_records"
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
CREATE POLICY "fund_argument_artifacts_founder_select" ON public."fund_argument_artifacts"
    FOR SELECT
    TO authenticated
    USING (EXISTS (SELECT 1 FROM public.fund_applications a JOIN public.fund_founders f ON f.id = a.founder_id WHERE a.id = application_id AND f.founder_user_id = auth.uid()::text));
CREATE POLICY "fund_argument_artifacts_service_all" ON public."fund_argument_artifacts"
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
"""
