"""Service for API key generation, verification, rotation, and revocation."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ApiKeyService:
    """Implements operational API key lifecycle."""

    @staticmethod
    def generate_token() -> str:
        return f"cfk_{secrets.token_urlsafe(32)}"

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    def create_key(
        self,
        repo: ApiKeyRepository,
        label: str,
        role: str,
        created_by: str,
        expires_in_days: Optional[int] = None,
    ) -> Dict[str, object]:
        token = self.generate_token()
        key_hash = self.hash_token(token)
        key_fingerprint = self.fingerprint(token)
        expires_at = None
        if expires_in_days is not None:
            expires_at = _utc_now() + timedelta(days=int(expires_in_days))
        rec = repo.create_key(
            label=label,
            role=role,
            key_hash=key_hash,
            key_fingerprint=key_fingerprint,
            created_by=created_by,
            expires_at=expires_at,
        )
        return {
            "id": rec.id,
            "token": token,
            "label": rec.label,
            "role": rec.role,
            "is_active": rec.is_active,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "fingerprint": rec.key_fingerprint,
            "created_at": rec.created_at.isoformat(),
        }

    def verify_token(self, repo: ApiKeyRepository, token: str) -> Dict[str, object]:
        key_hash = self.hash_token(token)
        rec = repo.get_by_hash(key_hash)
        if not rec:
            return {"ok": False, "reason": "unknown_key"}
        now = _utc_now()
        if not rec.is_active:
            return {"ok": False, "reason": "inactive", "key_id": rec.id}
        expires_at = rec.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at <= now:
            return {"ok": False, "reason": "expired", "key_id": rec.id}
        repo.mark_used(rec)
        return {
            "ok": True,
            "key_id": rec.id,
            "role": rec.role,
            "label": rec.label,
            "fingerprint": rec.key_fingerprint,
        }

    def revoke_key(self, repo: ApiKeyRepository, key_id: str) -> bool:
        rec = repo.get_by_id(key_id)
        if not rec:
            return False
        repo.revoke_key(rec)
        return True

    def rotate_key(
        self,
        repo: ApiKeyRepository,
        key_id: str,
        actor: str,
        expires_in_days: Optional[int] = None,
    ) -> Optional[Dict[str, object]]:
        old = repo.get_by_id(key_id)
        if not old:
            return None
        repo.revoke_key(old)
        return self.create_key(
            repo=repo,
            label=f"{old.label}-rotated",
            role=old.role,
            created_by=actor,
            expires_in_days=expires_in_days,
        )

