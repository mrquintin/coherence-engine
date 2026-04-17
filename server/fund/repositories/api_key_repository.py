"""Repository layer for DB-backed API key management."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ApiKeyRepository:
    """CRUD operations for API keys and audit events."""

    def __init__(self, db: Session):
        self.db = db

    def create_key(
        self,
        label: str,
        role: str,
        key_hash: str,
        key_fingerprint: str,
        created_by: str,
        expires_at: Optional[datetime],
    ) -> models.ApiKey:
        rec = models.ApiKey(
            id=_new_id("key"),
            label=label,
            role=role,
            key_hash=key_hash,
            key_fingerprint=key_fingerprint,
            is_active=True,
            expires_at=expires_at,
            revoked_at=None,
            created_by=created_by,
        )
        self.db.add(rec)
        self.db.flush()
        return rec

    def get_by_hash(self, key_hash: str) -> Optional[models.ApiKey]:
        stmt = select(models.ApiKey).where(models.ApiKey.key_hash == key_hash)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_id(self, key_id: str) -> Optional[models.ApiKey]:
        stmt = select(models.ApiKey).where(models.ApiKey.id == key_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_keys(self) -> List[models.ApiKey]:
        stmt = select(models.ApiKey).order_by(models.ApiKey.created_at.desc())
        return list(self.db.execute(stmt).scalars().all())

    def has_any_active_key(self) -> bool:
        stmt = (
            select(func.count(models.ApiKey.id))
            .where(models.ApiKey.is_active.is_(True))
        )
        count = self.db.execute(stmt).scalar_one()
        return int(count) > 0

    def mark_used(self, key: models.ApiKey) -> None:
        key.last_used_at = _utc_now()
        key.updated_at = _utc_now()
        self.db.flush()

    def revoke_key(self, key: models.ApiKey) -> None:
        key.is_active = False
        key.revoked_at = _utc_now()
        key.updated_at = _utc_now()
        self.db.flush()

    def add_audit_event(
        self,
        action: str,
        success: bool,
        actor: str,
        request_id: str,
        ip: str,
        path: str,
        details: dict,
        api_key_id: Optional[str] = None,
    ) -> models.ApiKeyAuditEvent:
        rec = models.ApiKeyAuditEvent(
            id=_new_id("ake"),
            api_key_id=api_key_id,
            action=action,
            success=success,
            actor=actor,
            request_id=request_id,
            ip=ip,
            path=path,
            details_json=json.dumps(details),
        )
        self.db.add(rec)
        self.db.flush()
        return rec

