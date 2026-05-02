"""Row-Level-Security (RLS) policy registry and applier.

This module declares — in code — the RLS policies the fund backend expects
on Postgres / Supabase. Two consumers:

* The Alembic migration ``20260425_000001_rls_policies`` calls
  :func:`apply_rls_policies` to install policies during ``alembic upgrade``.
* Tests pin the rendered DDL to detect drift between the declared registry
  and what migrations would emit.

Design notes
------------

* **Default deny.** Every table listed here gets ``ENABLE ROW LEVEL
  SECURITY`` with no permissive default — only the explicitly declared
  policies grant access.
* **service_role bypass.** The Supabase ``service_role`` key authenticates
  as a Postgres role that should retain full access for server-side code.
  Each protected table therefore gets a ``service_role ALL`` policy.
* **founder scoping.** End-user ``authenticated`` access is scoped through
  ``fund_applications.founder_id -> fund_founders.founder_user_id``. The
  database stores the Supabase ``sub`` claim on the founder row, not on each
  application row. Policies read the JWT ``sub`` claim from PostgREST request
  settings instead of depending on ``auth.uid()`` schema visibility, so an
  app-owned migration role can install them on Supabase.
* **SQLite is a no-op.** SQLite has no RLS concept; :func:`apply_rls_policies`
  detects the dialect and returns silently so unit tests / CI keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Sequence

from sqlalchemy.engine import Connection


RLSCommand = str  # "SELECT" | "INSERT" | "UPDATE" | "DELETE" | "ALL"

_VALID_COMMANDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "ALL"}
_POSTGRES_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class RLSPolicy:
    """A declarative Row-Level-Security policy.

    Attributes
    ----------
    table:
        Target table name (unqualified — schema is always ``public``).
    name:
        Policy name. Unique per table.
    command:
        SQL command this policy applies to: ``SELECT``, ``INSERT``,
        ``UPDATE``, ``DELETE``, or ``ALL``.
    using_clause:
        Expression placed in ``USING (...)``. Filters which existing rows
        are visible / mutable. Empty string means no USING clause is
        emitted (only valid for INSERT-only policies).
    with_check_clause:
        Expression placed in ``WITH CHECK (...)``. Validates new rows on
        INSERT / UPDATE. Empty string means no WITH CHECK clause.
    roles:
        Postgres role names this policy grants. ``["authenticated"]`` is
        the default Supabase end-user role; ``["service_role"]`` is the
        privileged server-side role.
    """

    table: str
    name: str
    command: RLSCommand
    using_clause: str = ""
    with_check_clause: str = ""
    roles: Sequence[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        cmd = self.command.upper()
        if cmd not in _VALID_COMMANDS:
            raise ValueError(
                f"RLSPolicy({self.table}.{self.name}): invalid command {self.command!r}"
            )
        if not self.roles:
            raise ValueError(
                f"RLSPolicy({self.table}.{self.name}): roles must be non-empty"
            )
        if cmd == "INSERT" and not self.with_check_clause:
            raise ValueError(
                f"RLSPolicy({self.table}.{self.name}): INSERT policy requires with_check_clause"
            )

    def render_create(self) -> str:
        """Render the ``CREATE POLICY`` DDL string (Postgres dialect)."""
        roles_sql = ", ".join(self.roles)
        parts = [
            f'CREATE POLICY "{self.name}" ON public."{self.table}"',
            f"    FOR {self.command.upper()}",
            f"    TO {roles_sql}",
        ]
        if self.using_clause:
            parts.append(f"    USING ({self.using_clause})")
        if self.with_check_clause:
            parts.append(f"    WITH CHECK ({self.with_check_clause})")
        return "\n".join(parts) + ";"

    def render_drop(self) -> str:
        """Render the idempotent ``DROP POLICY IF EXISTS`` DDL string."""
        return f'DROP POLICY IF EXISTS "{self.name}" ON public."{self.table}";'


# ---------------------------------------------------------------------------
# Policy registry
# ---------------------------------------------------------------------------

_CURRENT_JWT_SUB = (
    "COALESCE("
    "NULLIF(current_setting('request.jwt.claim.sub', true), ''), "
    "NULLIF(NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub', '')"
    ")"
)

_FOUNDER_OWNS_CURRENT_APPLICATION = (
    "EXISTS (SELECT 1 FROM public.fund_founders f "
    f"WHERE f.id = founder_id AND f.founder_user_id = {_CURRENT_JWT_SUB})"
)

_FOUNDER_OWNS_APPLICATION = (
    "EXISTS (SELECT 1 FROM public.fund_applications a "
    "JOIN public.fund_founders f ON f.id = a.founder_id "
    f"WHERE a.id = application_id AND f.founder_user_id = {_CURRENT_JWT_SUB})"
)

RLS_POLICIES: list[RLSPolicy] = [
    # fund_applications: founders see their own rows; service_role full access.
    RLSPolicy(
        table="fund_applications",
        name="fund_applications_founder_select",
        command="SELECT",
        using_clause=_FOUNDER_OWNS_CURRENT_APPLICATION,
        roles=("authenticated",),
    ),
    RLSPolicy(
        table="fund_applications",
        name="fund_applications_service_all",
        command="ALL",
        using_clause="true",
        with_check_clause="true",
        roles=("service_role",),
    ),
    # fund_decisions: founders see decisions tied to their applications.
    RLSPolicy(
        table="fund_decisions",
        name="fund_decisions_founder_select",
        command="SELECT",
        using_clause=_FOUNDER_OWNS_APPLICATION,
        roles=("authenticated",),
    ),
    RLSPolicy(
        table="fund_decisions",
        name="fund_decisions_service_all",
        command="ALL",
        using_clause="true",
        with_check_clause="true",
        roles=("service_role",),
    ),
    # fund_event_outbox: server-side only.
    RLSPolicy(
        table="fund_event_outbox",
        name="fund_event_outbox_service_all",
        command="ALL",
        using_clause="true",
        with_check_clause="true",
        roles=("service_role",),
    ),
    # fund_idempotency_records: server-side only.
    RLSPolicy(
        table="fund_idempotency_records",
        name="fund_idempotency_records_service_all",
        command="ALL",
        using_clause="true",
        with_check_clause="true",
        roles=("service_role",),
    ),
    # fund_argument_artifacts: founder access scoped via owning application.
    RLSPolicy(
        table="fund_argument_artifacts",
        name="fund_argument_artifacts_founder_select",
        command="SELECT",
        using_clause=_FOUNDER_OWNS_APPLICATION,
        roles=("authenticated",),
    ),
    RLSPolicy(
        table="fund_argument_artifacts",
        name="fund_argument_artifacts_service_all",
        command="ALL",
        using_clause="true",
        with_check_clause="true",
        roles=("service_role",),
    ),
]


# ---------------------------------------------------------------------------
# PII clear-read audit log policies (prompt 58)
# ---------------------------------------------------------------------------
#
# Held in a sibling registry rather than appended to ``RLS_POLICIES`` so the
# pinned-snapshot test against the prompt-22 surface keeps holding. The
# ``20260425_000015`` migration installs both registries on Postgres.
#
# Tampering protection contract for ``pii_clear_audit_log``:
#
# * service_role: INSERT only. The application server (running as
#   ``service_role``) is the only writer.
# * service_role + admin: SELECT. The audit table is queryable by the
#   server itself and by an operator dashboard.
# * No UPDATE / DELETE policy is declared for any role. Combined with
#   default-deny RLS this means no role -- including ``service_role``
#   -- may modify or remove an audit row through the policy surface.
#   A defence-in-depth trigger in the same migration raises on any
#   UPDATE / DELETE attempt at the table level, catching attempts that
#   bypass RLS (e.g. via a superuser).

PII_AUDIT_RLS_POLICIES: list[RLSPolicy] = [
    RLSPolicy(
        table="pii_clear_audit_log",
        name="pii_clear_audit_log_service_insert",
        command="INSERT",
        with_check_clause="true",
        roles=("service_role",),
    ),
    RLSPolicy(
        table="pii_clear_audit_log",
        name="pii_clear_audit_log_service_select",
        command="SELECT",
        using_clause="true",
        roles=("service_role",),
    ),
    RLSPolicy(
        table="pii_clear_audit_log",
        name="pii_clear_audit_log_admin_select",
        command="SELECT",
        using_clause="true",
        roles=("admin",),
    ),
]


def rls_tables(policies: Iterable[RLSPolicy] = RLS_POLICIES) -> list[str]:
    """Return the unique, ordered list of tables covered by ``policies``."""
    seen: dict[str, None] = {}
    for p in policies:
        seen.setdefault(p.table, None)
    return list(seen.keys())


def render_enable_rls(table: str) -> str:
    """Render the ``ENABLE ROW LEVEL SECURITY`` DDL for ``table``."""
    return f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY;'


def render_disable_rls(table: str) -> str:
    """Render the ``DISABLE ROW LEVEL SECURITY`` DDL for ``table``."""
    return f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY;'


def render_policy_ddl(policies: Iterable[RLSPolicy] = RLS_POLICIES) -> str:
    """Render the full upgrade DDL block (enable + drop-if-exists + create).

    Used by tests as a snapshot anchor. The drop-then-create pattern keeps
    the migration idempotent across reruns on Postgres.
    """
    policies = list(policies)
    lines: list[str] = []
    for tbl in rls_tables(policies):
        lines.append(render_enable_rls(tbl))
    lines.append("")
    for p in policies:
        lines.append(p.render_drop())
    lines.append("")
    for p in policies:
        lines.append(p.render_create())
    return "\n".join(lines).rstrip() + "\n"


def _quote_postgres_ident(identifier: str) -> str:
    if not _POSTGRES_IDENT_RE.fullmatch(identifier):
        raise ValueError(f"Unsafe Postgres identifier in RLS registry: {identifier!r}")
    return f'"{identifier}"'


def ensure_postgres_rls_prerequisites(
    connection: Connection,
    policies: Iterable[RLSPolicy] = RLS_POLICIES,
) -> None:
    """Create Supabase-compatible RLS support objects when missing.

    Supabase already provides the ``authenticated`` / ``service_role`` roles.
    The migration gate runs against a plain Postgres service container, so
    the same policy DDL needs compatibility roles there. This helper is
    intentionally additive and does not drop anything on downgrade.
    """
    from sqlalchemy import text

    dialect = connection.dialect.name.lower()
    if dialect != "postgresql":
        return

    policies = list(policies)
    roles = sorted({role for policy in policies for role in policy.roles})
    for role in roles:
        quoted_role = _quote_postgres_ident(role)
        connection.execute(
            text(
                f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {quoted_role} NOLOGIN;
    END IF;
END
$$;
"""
            )
        )

    if any("auth.uid()" in p.using_clause or "auth.uid()" in p.with_check_clause for p in policies):
        connection.execute(
            text(
                """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth') THEN
        CREATE SCHEMA auth;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'auth'
          AND p.proname = 'uid'
          AND pg_get_function_identity_arguments(p.oid) = ''
    ) THEN
        CREATE FUNCTION auth.uid()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $fn$ SELECT NULL::uuid $fn$;
    END IF;
END
$$;
"""
            )
        )


def apply_rls_policies(
    connection: Connection,
    policies: Iterable[RLSPolicy] = RLS_POLICIES,
) -> None:
    """Idempotently enable RLS and install ``policies`` on ``connection``.

    On SQLite (and any non-Postgres dialect) this is a no-op — SQLite has
    no row-level security mechanism, so unit tests / CI can call this
    safely without raising.

    On Postgres the function:

    1. Issues ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` per table.
    2. Drops each declared policy if it already exists, then creates it.

    The caller owns the transaction; this function does not commit.
    """
    from sqlalchemy import text

    dialect = connection.dialect.name.lower()
    if dialect != "postgresql":
        return

    policies = list(policies)
    ensure_postgres_rls_prerequisites(connection, policies)
    for tbl in rls_tables(policies):
        connection.execute(text(render_enable_rls(tbl)))
    for p in policies:
        connection.execute(text(p.render_drop()))
        connection.execute(text(p.render_create()))


def revert_rls_policies(
    connection: Connection,
    policies: Iterable[RLSPolicy] = RLS_POLICIES,
) -> None:
    """Drop ``policies`` and disable RLS. No-op outside Postgres."""
    from sqlalchemy import text

    dialect = connection.dialect.name.lower()
    if dialect != "postgresql":
        return

    policies = list(policies)
    for p in policies:
        connection.execute(text(p.render_drop()))
    for tbl in rls_tables(policies):
        connection.execute(text(render_disable_rls(tbl)))
