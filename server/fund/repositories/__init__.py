"""Repository layer package.

Read-replica routing convention
-------------------------------

Repositories receive a primary :class:`sqlalchemy.orm.Session` via their
constructor, used unconditionally for write paths. Pure-read methods may
additionally accept a keyword-only ``session=...`` to override the
primary, plus a ``read_only=False`` flag.

When ``read_only=True`` and no explicit session is passed, the call opens
a new session via
:func:`coherence_engine.server.fund.database.SessionFactory` with
``read_only=True``, executes the read for the duration of the call, and
closes it. This routes the query to the read replica when
``SUPABASE_DB_REPLICA_URL`` is configured, and transparently falls back
to the primary engine when it is not.

Write methods MUST always use the primary session — replicas are
read-only by definition and writing to one is an error, not a fallback.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy.orm import Session


@contextmanager
def resolve_read_session(
    explicit_session: Optional[Session],
    primary_session: Optional[Session],
    *,
    read_only: bool = False,
) -> Iterator[Session]:
    """Yield the session a repository read method should use.

    Resolution order:

    1. ``explicit_session`` if provided — caller-managed lifetime.
    2. ``primary_session`` if provided — repository-managed lifetime.
    3. A freshly opened replica/primary session via ``SessionFactory``,
       closed when the context exits.

    Only path (3) opens (and is therefore responsible for closing) a
    session. Paths (1) and (2) yield the caller's / repository's
    existing session unchanged.
    """
    if explicit_session is not None:
        yield explicit_session
        return
    if primary_session is not None:
        yield primary_session
        return

    # Local import to avoid circular import at package load time.
    from coherence_engine.server.fund.database import SessionFactory

    session = SessionFactory(read_only=read_only)
    try:
        yield session
    finally:
        session.close()


__all__ = ["resolve_read_session"]
