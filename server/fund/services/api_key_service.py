"""API key service — Argon2id-hashed credentials with explicit scopes (prompt 28).

The v2 model:

* Tokens are formatted ``ce_<prefix>_<secret>`` where ``prefix`` is an
  8-character public discriminator used for O(1) row lookup, and
  ``secret`` is 32 bytes of base64url-encoded entropy.
* The full presented token is hashed with **Argon2id** (never SHA-256;
  SHA family digests are not credential-hash primitives).
* Plaintext tokens are surfaced exactly **once** at create / rotate time
  and never persisted.
* Each key carries an explicit list of scopes drawn from
  :data:`KNOWN_SCOPES`, a per-key rate limit, and a hard 1-year default
  expiry.

Legacy ``create_key`` / ``verify_token`` / ``revoke_key`` / ``rotate_key``
methods are retained with their original signatures so the existing
admin UI router, fund middleware, and workflow router (none of which
are in this prompt's scope) keep working. They now derive a v2 key under
the hood: a random scope set chosen from ``role`` is attached, the
plaintext is Argon2id-hashed, and the legacy compatibility columns
(``role``, ``key_fingerprint`` mirroring ``prefix``) are populated for
back-compat.
"""

from __future__ import annotations

import base64
import json
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from sqlalchemy import select
from sqlalchemy.orm import Session

from coherence_engine.server.fund import models
from coherence_engine.server.fund.repositories.api_key_repository import ApiKeyRepository


KNOWN_SCOPES: frozenset = frozenset(
    {
        "applications:read",
        "applications:write",
        "decisions:read",
        "admin:read",
        "admin:write",
        "worker:claim",
        "worker:complete",
    }
)

# Coarse legacy-role → v2 scope mapping. Used only by the legacy
# ``create_key(label=, role=)`` shim so existing tests / middleware
# continue to authorize the same surfaces.
_ROLE_SCOPES: Dict[str, List[str]] = {
    "admin": [
        "applications:read",
        "applications:write",
        "decisions:read",
        "admin:read",
        "admin:write",
        "worker:claim",
        "worker:complete",
    ],
    "analyst": [
        "applications:read",
        "applications:write",
        "decisions:read",
    ],
    "viewer": [
        "applications:read",
        "decisions:read",
    ],
    "worker": ["worker:claim", "worker:complete"],
    "service": [],
}


DEFAULT_EXPIRY_DAYS = 365
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
PREFIX_LENGTH = 8
PREFIX_ALPHABET = string.ascii_lowercase + string.digits

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    type=Type.ID,
)


class InvalidKey(Exception):
    """Raised when a presented token cannot be authenticated."""


@dataclass
class CreatedKey:
    """Plaintext-bearing result of a ``create_key_v2`` / ``rotate_key_v2`` call.

    The ``token`` field is the only place the plaintext key ever exists
    server-side and the caller is responsible for forwarding it to the
    operator immediately. Subsequent reads of the row return only the
    ``prefix``.
    """

    id: str
    token: str
    prefix: str
    service_account_id: Optional[str]
    scopes: List[str]
    expires_at: Optional[datetime]
    rate_limit_per_minute: int


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _generate_prefix() -> str:
    return "".join(secrets.choice(PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))


def _generate_secret_bytes() -> str:
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _validate_scopes(scopes: Sequence[str]) -> List[str]:
    cleaned: List[str] = []
    for s in scopes:
        s_norm = s.strip()
        if not s_norm:
            continue
        if s_norm not in KNOWN_SCOPES:
            raise ValueError(f"unknown scope: {s_norm!r}")
        cleaned.append(s_norm)
    # Preserve insertion order; deduplicate while keeping first occurrence.
    seen = set()
    unique: List[str] = []
    for s in cleaned:
        if s in seen:
            continue
        seen.add(s)
        unique.append(s)
    return unique


def _split_token(token: str) -> Optional[str]:
    """Return the 8-char ``prefix`` embedded in a ``ce_<prefix>_<secret>`` token, or ``None``."""
    if not isinstance(token, str):
        return None
    if not token.startswith("ce_"):
        return None
    parts = token.split("_", 2)
    if len(parts) != 3:
        return None
    prefix = parts[1]
    if len(prefix) != PREFIX_LENGTH:
        return None
    if any(c not in PREFIX_ALPHABET for c in prefix):
        return None
    return prefix


def _coarse_role_for_scopes(scopes: Sequence[str]) -> str:
    """Pick a single legacy ``role`` value that summarizes a v2 scope set.

    Used purely so the legacy ``role`` column on ``ApiKey`` keeps a
    sensible value while transitional code paths still gate on it. Order
    matters: ``admin`` wins over ``analyst`` wins over ``viewer`` wins
    over ``worker``.
    """

    s = set(scopes)
    if {"admin:write", "admin:read"} & s:
        return "admin"
    if "applications:write" in s:
        return "analyst"
    if {"worker:claim", "worker:complete"} & s:
        return "worker"
    if "applications:read" in s or "decisions:read" in s:
        return "viewer"
    return "service"


class ApiKeyService:
    """Lifecycle operations for v2 API keys (Argon2id, scoped, rate-limited)."""

    # ------------------------------------------------------------------
    # v2 public API
    # ------------------------------------------------------------------
    def create_key_v2(
        self,
        db: Session,
        *,
        service_account_id: Optional[str],
        scopes: Sequence[str],
        created_by: str,
        label: str = "",
        expires_at: Optional[datetime] = None,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    ) -> CreatedKey:
        scopes_clean = _validate_scopes(scopes)
        if expires_at is None:
            expires_at = _utc_now() + timedelta(days=DEFAULT_EXPIRY_DAYS)

        prefix = _generate_prefix()
        # Probability of a prefix collision is negligible (36**8) but
        # retry once if we somehow hit one — easier than carrying the
        # error all the way back to the operator.
        for _ in range(3):
            existing = db.execute(
                select(models.ApiKey.id).where(models.ApiKey.prefix == prefix)
            ).first()
            if existing is None:
                break
            prefix = _generate_prefix()

        secret = _generate_secret_bytes()
        token = f"ce_{prefix}_{secret}"
        key_hash = _PASSWORD_HASHER.hash(token)

        rec = models.ApiKey(
            id=_new_id("key"),
            service_account_id=service_account_id,
            prefix=prefix,
            scopes_json=json.dumps(scopes_clean),
            rate_limit_per_minute=int(rate_limit_per_minute),
            label=label or "",
            role=_coarse_role_for_scopes(scopes_clean),
            key_hash=key_hash,
            key_fingerprint=prefix,
            is_active=True,
            expires_at=expires_at,
            revoked_at=None,
            created_by=created_by,
            last_used_at=None,
        )
        db.add(rec)
        db.flush()
        return CreatedKey(
            id=rec.id,
            token=token,
            prefix=prefix,
            service_account_id=service_account_id,
            scopes=scopes_clean,
            expires_at=expires_at,
            rate_limit_per_minute=int(rate_limit_per_minute),
        )

    def verify_key(self, db: Session, presented: str) -> models.ApiKey:
        """Return the matching :class:`ApiKey` row or raise :class:`InvalidKey`.

        Matching is constant-time on the Argon2id step. Expiry and
        revocation are *also* surfaced as :class:`InvalidKey` (with a
        machine-readable ``reason`` attribute) so callers can map them to
        a uniform 401 with distinct error codes.
        """

        prefix = _split_token(presented)
        if prefix is None:
            raise self._invalid("malformed_token")

        rec: Optional[models.ApiKey] = db.execute(
            select(models.ApiKey).where(models.ApiKey.prefix == prefix)
        ).scalar_one_or_none()
        if rec is None:
            # Force a constant-time hash even on prefix miss to avoid a
            # timing oracle on prefix existence.
            try:
                _PASSWORD_HASHER.verify(_DUMMY_HASH, presented)
            except Exception:
                pass
            raise self._invalid("unknown_key")

        try:
            _PASSWORD_HASHER.verify(rec.key_hash, presented)
        except (VerifyMismatchError, InvalidHashError):
            raise self._invalid("unknown_key") from None

        if rec.revoked_at is not None:
            raise self._invalid("revoked", key_id=rec.id, prefix=rec.prefix)
        expires_at = rec.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at is not None and expires_at <= _utc_now():
            raise self._invalid("expired", key_id=rec.id, prefix=rec.prefix)
        return rec

    def mark_used(self, db: Session, rec: models.ApiKey) -> None:
        rec.last_used_at = _utc_now()
        rec.updated_at = _utc_now()
        db.flush()

    def revoke_key_v2(self, db: Session, prefix: str) -> Optional[models.ApiKey]:
        rec = db.execute(
            select(models.ApiKey).where(models.ApiKey.prefix == prefix)
        ).scalar_one_or_none()
        if rec is None:
            return None
        rec.revoked_at = _utc_now()
        rec.is_active = False
        rec.updated_at = _utc_now()
        db.flush()
        return rec

    def rotate_key_v2(
        self,
        db: Session,
        *,
        prefix: str,
        actor: str,
        grace_seconds: int = 0,
        expires_at: Optional[datetime] = None,
    ) -> Optional[CreatedKey]:
        """Issue a new key with the same scopes / rate limit / service account.

        The old key is *not* immediately revoked; instead its
        ``expires_at`` is shrunk to ``now + grace_seconds`` so callers
        with the old token in flight have a deterministic cut-over
        window. ``grace_seconds=0`` (default) revokes immediately.
        """

        old = db.execute(
            select(models.ApiKey).where(models.ApiKey.prefix == prefix)
        ).scalar_one_or_none()
        if old is None:
            return None

        new_key = self.create_key_v2(
            db,
            service_account_id=old.service_account_id,
            scopes=json.loads(old.scopes_json or "[]"),
            created_by=actor,
            label=old.label,
            expires_at=expires_at,
            rate_limit_per_minute=old.rate_limit_per_minute,
        )

        if grace_seconds <= 0:
            old.revoked_at = _utc_now()
            old.is_active = False
        else:
            cutoff = _utc_now() + timedelta(seconds=int(grace_seconds))
            # Only shrink the expiry, never extend it.
            current = old.expires_at
            if current is not None and current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if current is None or cutoff < current:
                old.expires_at = cutoff
        old.updated_at = _utc_now()
        db.flush()
        return new_key

    @staticmethod
    def _invalid(reason: str, **extra) -> InvalidKey:
        err = InvalidKey(reason)
        err.reason = reason  # type: ignore[attr-defined]
        for k, v in extra.items():
            setattr(err, k, v)
        return err

    # ------------------------------------------------------------------
    # Legacy compatibility surface (signature-preserving wrappers).
    #
    # Pre-prompt-28 callers (admin UI, workflow router, fund middleware)
    # construct an ``ApiKeyRepository`` and call these methods. They keep
    # working but now produce / verify Argon2id-hashed v2 keys.
    # ------------------------------------------------------------------
    @staticmethod
    def fingerprint(token: str) -> str:
        prefix = _split_token(token)
        return prefix if prefix is not None else ""

    def create_key(
        self,
        repo: ApiKeyRepository,
        label: str,
        role: str,
        created_by: str,
        expires_in_days: Optional[int] = None,
    ) -> Dict[str, object]:
        scopes = _ROLE_SCOPES.get(role.lower())
        if scopes is None:
            scopes = []
        expires_at: Optional[datetime] = None
        if expires_in_days is not None:
            expires_at = _utc_now() + timedelta(days=int(expires_in_days))
        else:
            expires_at = _utc_now() + timedelta(days=DEFAULT_EXPIRY_DAYS)
        created = self.create_key_v2(
            repo.db,
            service_account_id=None,
            scopes=scopes,
            created_by=created_by,
            label=label,
            expires_at=expires_at,
        )
        rec = repo.db.execute(
            select(models.ApiKey).where(models.ApiKey.id == created.id)
        ).scalar_one()
        # Preserve the requested legacy role on the row even when the
        # coarse mapping would have picked something different.
        if role:
            rec.role = role
            repo.db.flush()
        return {
            "id": rec.id,
            "token": created.token,
            "label": rec.label,
            "role": rec.role,
            "is_active": rec.is_active,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "fingerprint": rec.prefix,
            "scopes": list(created.scopes),
            "created_at": rec.created_at.isoformat(),
        }

    def verify_token(
        self, repo: ApiKeyRepository, token: str
    ) -> Dict[str, object]:
        try:
            rec = self.verify_key(repo.db, token)
        except InvalidKey as exc:
            reason = getattr(exc, "reason", "unknown_key")
            payload: Dict[str, object] = {"ok": False, "reason": reason}
            key_id = getattr(exc, "key_id", None)
            if key_id is not None:
                payload["key_id"] = key_id
            return payload
        self.mark_used(repo.db, rec)
        return {
            "ok": True,
            "key_id": rec.id,
            "role": rec.role,
            "label": rec.label,
            "fingerprint": rec.prefix,
            "scopes": json.loads(rec.scopes_json or "[]"),
        }

    def revoke_key(self, repo: ApiKeyRepository, key_id: str) -> bool:
        rec = repo.db.execute(
            select(models.ApiKey).where(models.ApiKey.id == key_id)
        ).scalar_one_or_none()
        if rec is None:
            return False
        rec.revoked_at = _utc_now()
        rec.is_active = False
        rec.updated_at = _utc_now()
        repo.db.flush()
        return True

    def rotate_key(
        self,
        repo: ApiKeyRepository,
        key_id: str,
        actor: str,
        expires_in_days: Optional[int] = None,
    ) -> Optional[Dict[str, object]]:
        old = repo.db.execute(
            select(models.ApiKey).where(models.ApiKey.id == key_id)
        ).scalar_one_or_none()
        if old is None:
            return None
        expires_at: Optional[datetime] = None
        if expires_in_days is not None:
            expires_at = _utc_now() + timedelta(days=int(expires_in_days))
        new_key = self.rotate_key_v2(
            repo.db, prefix=old.prefix, actor=actor, expires_at=expires_at
        )
        if new_key is None:
            return None
        rec = repo.db.execute(
            select(models.ApiKey).where(models.ApiKey.id == new_key.id)
        ).scalar_one()
        return {
            "id": rec.id,
            "token": new_key.token,
            "label": rec.label,
            "role": rec.role,
            "is_active": rec.is_active,
            "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            "fingerprint": rec.prefix,
            "scopes": list(new_key.scopes),
            "created_at": rec.created_at.isoformat(),
        }


# A pre-computed Argon2id hash of a value no caller will ever submit. We
# verify against this on prefix miss so the hash branch is always taken
# and timing of "unknown prefix" matches "wrong secret".
_DUMMY_HASH = _PASSWORD_HASHER.hash("ce_xxxxxxxx_dummy_constant_time_padding")
