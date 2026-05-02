"""Read-replica-aware view layer for ``fund_applications`` and decisions.

This module is intentionally distinct from
:mod:`coherence_engine.server.fund.repositories.application_repository`,
which holds the full lifecycle (write) repository. ``ApplicationsReadRepository``
exposes only the pure-read query surface needed by API list / detail
endpoints, and routes through the read replica when one is configured.

The split mirrors the broader convention introduced in this prompt:

* Write paths use the primary :class:`Session` passed to the lifecycle
  repository.
* Read-only API queries can be opted in to the replica via
  ``read_only=True``, which (transparently) falls back to the primary
  engine when ``SUPABASE_DB_REPLICA_URL`` is not set.

Rule of thumb: read here is safe when the caller can tolerate a few
seconds of replication lag. Anything written in the same transaction as
the read MUST stay on the primary — see
``docs/specs/db_pooling_and_retries.md`` for the full SLA.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.repositories import resolve_read_session


class ApplicationsReadRepository:
    """Replica-routed reads over applications and their decisions."""

    def __init__(self, db: Optional[Session] = None):
        self.db = db

    def get_application(
        self,
        application_id: str,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> Optional[models.Application]:
        stmt = select(models.Application).where(models.Application.id == application_id)
        with resolve_read_session(session, self.db, read_only=read_only) as db:
            return db.execute(stmt).scalar_one_or_none()

    def get_decision(
        self,
        application_id: str,
        *,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> Optional[models.Decision]:
        stmt = select(models.Decision).where(models.Decision.application_id == application_id)
        with resolve_read_session(session, self.db, read_only=read_only) as db:
            return db.execute(stmt).scalar_one_or_none()

    def list_applications_by_founder(
        self,
        founder_id: str,
        *,
        limit: int = 100,
        session: Optional[Session] = None,
        read_only: bool = False,
    ) -> List[models.Application]:
        stmt = (
            select(models.Application)
            .where(models.Application.founder_id == founder_id)
            .order_by(models.Application.created_at.desc())
            .limit(max(1, int(limit)))
        )
        with resolve_read_session(session, self.db, read_only=read_only) as db:
            return list(db.execute(stmt).scalars().all())


__all__ = ["ApplicationsReadRepository"]
