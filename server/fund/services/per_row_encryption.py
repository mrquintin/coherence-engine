"""Per-row authenticated encryption with crypto-shreddable keys (prompt 57).

High-PII rows (transcripts, recordings, KYC evidence, founder PII) hold an
opaque ``key_id`` pointing into the ``fund_encryption_keys`` table. The
ciphertext column stores ``b64( version || nonce(12) || aesgcm_ct_with_tag )``
and is unrecoverable once the matching key row is shredded
(:mod:`server.fund.services.crypto_shred`).

The cipher is **AES-256-GCM** with a fresh 96-bit nonce per encryption
(generated from ``os.urandom``) and the row's logical id used as
``associated_data`` so a swapped ciphertext between rows still fails
authentication. Reuse of (key, nonce) is catastrophic for AES-GCM; the
helper never accepts a caller-supplied nonce.

Storage of key material
-----------------------

The default :class:`EncryptionKeyStore` reads / writes
:class:`coherence_engine.server.fund.models.EncryptionKey` rows: ``id``
(opaque key_id), ``key_material_b64`` (32-byte AES key, base64), and
``shredded_at`` (set by :func:`crypto_shred.shred_key`). Production
deployments may inject a KMS-backed store via
:func:`set_encryption_key_store`; the contract is the same.
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from typing import Optional, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session


_VERSION = b"\x01"
_NONCE_BYTES = 12
_KEY_BYTES = 32  # AES-256


class EncryptionError(RuntimeError):
    """Base class for encryption failures."""


class KeyShreddedError(EncryptionError):
    """Raised when decryption is attempted against a shredded key."""


class KeyNotFoundError(EncryptionError):
    """Raised when ``key_id`` does not resolve to any known key."""


class CiphertextCorrupt(EncryptionError):
    """Raised when AES-GCM authentication fails (tag mismatch / tampering)."""


@dataclass(frozen=True)
class KeyMaterial:
    """A live key record returned by the key store."""

    key_id: str
    raw: bytes  # 32 bytes for AES-256


class EncryptionKeyStore(Protocol):
    """The contract for resolving ``key_id`` → key bytes."""

    def get(self, db: Session, key_id: str) -> KeyMaterial:
        """Return the key material; raise :class:`KeyShreddedError` if shredded."""

    def create(self, db: Session) -> KeyMaterial:
        """Mint a fresh 32-byte AES-256 key and persist it."""


class _ModelKeyStore:
    """Default :class:`EncryptionKeyStore` backed by ``models.EncryptionKey``."""

    def get(self, db: Session, key_id: str) -> KeyMaterial:
        from coherence_engine.server.fund import models  # local import: model lives here

        row = db.get(models.EncryptionKey, key_id)
        if row is None:
            raise KeyNotFoundError(f"unknown key_id: {key_id!r}")
        if row.shredded_at is not None or not row.key_material_b64:
            raise KeyShreddedError(f"key {key_id!r} has been shredded")
        try:
            raw = base64.b64decode(row.key_material_b64.encode("ascii"))
        except Exception as exc:  # pragma: no cover - defensive
            raise EncryptionError(f"key {key_id!r} stored material is malformed") from exc
        if len(raw) != _KEY_BYTES:
            raise EncryptionError(
                f"key {key_id!r} length {len(raw)} != expected {_KEY_BYTES}"
            )
        return KeyMaterial(key_id=key_id, raw=raw)

    def create(self, db: Session) -> KeyMaterial:
        from coherence_engine.server.fund import models

        raw = secrets.token_bytes(_KEY_BYTES)
        key_id = f"key_{uuid.uuid4().hex}"
        row = models.EncryptionKey(
            id=key_id,
            key_material_b64=base64.b64encode(raw).decode("ascii"),
        )
        db.add(row)
        db.flush()
        return KeyMaterial(key_id=key_id, raw=raw)


_STORE_LOCK = threading.Lock()
_STORE: EncryptionKeyStore = _ModelKeyStore()


def get_encryption_key_store() -> EncryptionKeyStore:
    return _STORE


def set_encryption_key_store(store: Optional[EncryptionKeyStore]) -> None:
    """Replace the process-wide key store (test / KMS injection)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store if store is not None else _ModelKeyStore()


def _aad(row_id: str) -> bytes:
    """Bind ciphertext to its row id so swapped blobs fail authentication."""
    return row_id.encode("utf-8")


def encrypt(
    plaintext: bytes,
    *,
    db: Session,
    row_id: str,
    key_id: Optional[str] = None,
) -> tuple[str, str]:
    """Encrypt ``plaintext`` under a per-row AES-GCM key.

    If ``key_id`` is ``None`` the store mints a fresh key and the new id
    is returned alongside the base64 ciphertext. Callers persist both
    the ciphertext column and the ``key_id`` column on the same row.

    Returns ``(key_id, ciphertext_b64)``.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes")
    if not row_id:
        raise ValueError("row_id is required (used as AES-GCM AAD)")
    store = get_encryption_key_store()
    material = store.get(db, key_id) if key_id else store.create(db)
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(material.raw).encrypt(nonce, bytes(plaintext), _aad(row_id))
    blob = _VERSION + nonce + ct
    return material.key_id, base64.b64encode(blob).decode("ascii")


def decrypt(
    ciphertext_b64: str,
    *,
    db: Session,
    row_id: str,
    key_id: str,
) -> bytes:
    """Decrypt a base64 ciphertext produced by :func:`encrypt`.

    Raises :class:`KeyShreddedError` when the per-row key has been
    shredded — callers should map this to ``HTTP 410 Gone`` for read
    endpoints.
    """
    store = get_encryption_key_store()
    material = store.get(db, key_id)
    try:
        blob = base64.b64decode(ciphertext_b64.encode("ascii"))
    except Exception as exc:
        raise CiphertextCorrupt(f"invalid base64 for row {row_id!r}") from exc
    if len(blob) < 1 + _NONCE_BYTES + 16:
        raise CiphertextCorrupt(f"ciphertext too short for row {row_id!r}")
    if blob[0:1] != _VERSION:
        raise CiphertextCorrupt(f"unsupported ciphertext version for row {row_id!r}")
    nonce = blob[1 : 1 + _NONCE_BYTES]
    ct = blob[1 + _NONCE_BYTES :]
    try:
        return AESGCM(material.raw).decrypt(nonce, ct, _aad(row_id))
    except Exception as exc:
        raise CiphertextCorrupt(
            f"AES-GCM authentication failed for row {row_id!r}: {exc}"
        ) from exc
