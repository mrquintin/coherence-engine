"""Format-preserving PII tokenization (prompt 58).

Sensitive PII fields (email, full legal name, phone, residence address)
are stored on disk as deterministic, non-reversible tokens of the form::

    tok_<kind>_<32-hex>

The hex segment is the first 16 bytes of an HMAC-SHA-256 over the value
keyed by a per-tenant salt held in the secret manager under the name
``PII_TENANT_SALT``. Properties:

* **Deterministic per tenant.** The same ``(value, kind, tenant_salt)``
  triple always produces the same token, so equality joins, dedup, and
  CRM lookups continue to work without ever loading clear PII.
* **Non-reversible.** The clear value cannot be recovered from a token
  without the salt, and HMAC-SHA-256 has no usable preimage attack.
* **Domain-separated by kind.** Tokens for different kinds of PII
  (``email`` vs ``phone``) cannot collide even if the underlying values
  happen to be identical strings, because ``kind`` is mixed into the HMAC
  input.

The clear values are NOT thrown away — they are stored in a sibling
``*_clear`` column under per-row AES-GCM encryption (see
:mod:`per_row_encryption`) and are reachable only through the
:mod:`pii_clear_audit` API gate, which requires the ``pii:read_clear``
scope and writes an immutable audit-log row per access.

Salt rotation is operator-driven and documented in
``docs/specs/pii_handling.md``: rotating ``PII_TENANT_SALT`` invalidates
*every* existing token, so the rotation play also runs the documented
re-tokenization migration to recompute every ``*_token`` column under
the new salt.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from coherence_engine.server.fund.services.secret_manager import (
    get_secret_resolver,
)


PII_TENANT_SALT_NAME = "PII_TENANT_SALT"

# The fixed prefix on every token — callers can use this to assert that
# they have a token rather than a clear value before logging or writing
# the value to a downstream system.
TOKEN_PREFIX = "tok_"

# Allowed ``kind`` discriminators. Restricted to a closed set so a typo
# at a call site is caught immediately rather than producing an
# orphaned token namespace nobody indexes.
KNOWN_KINDS: frozenset[str] = frozenset(
    {"email", "name", "phone", "address"}
)


class PIITokenizationError(RuntimeError):
    """Raised when tokenization is attempted with no resolvable salt."""


def _resolve_salt(explicit: Optional[str]) -> bytes:
    """Return the tenant salt bytes — explicit override beats env beats secret manager."""
    if explicit is not None:
        if not explicit:
            raise PIITokenizationError("explicit tenant_salt must be non-empty")
        return explicit.encode("utf-8")

    env_salt = os.getenv(PII_TENANT_SALT_NAME, "").strip()
    if env_salt:
        return env_salt.encode("utf-8")

    resolver = get_secret_resolver()
    secret = resolver.get(PII_TENANT_SALT_NAME) if resolver is not None else None
    if not secret:
        raise PIITokenizationError(
            f"missing required secret {PII_TENANT_SALT_NAME!r}; "
            f"configure it in the secret manager before tokenizing PII"
        )
    return secret.encode("utf-8")


def hmac_sha256(salt: bytes, value: bytes) -> bytes:
    """Return the HMAC-SHA-256 of ``value`` keyed by ``salt``."""
    return hmac.new(salt, value, hashlib.sha256).digest()


def tokenize(
    value: str,
    *,
    kind: str,
    tenant_salt: Optional[str] = None,
) -> str:
    """Tokenize ``value`` of the given ``kind`` under the tenant salt.

    Returns ``"tok_<kind>_<32hex>"``. Empty / whitespace-only input
    raises :class:`ValueError` — empty PII fields stay empty rather than
    being tokenized into a single global "empty" token (which would let
    an attacker correlate empty values across tenants).
    """
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    norm = value.strip().lower() if kind == "email" else value.strip()
    if not norm:
        raise ValueError("cannot tokenize empty value")
    kind_clean = kind.strip().lower()
    if kind_clean not in KNOWN_KINDS:
        raise ValueError(
            f"unknown PII kind {kind!r}; allowed: {sorted(KNOWN_KINDS)}"
        )

    salt = _resolve_salt(tenant_salt)
    # Domain-separate kinds: equal values across kinds must not collide.
    msg = (kind_clean + ":" + norm).encode("utf-8")
    digest = hmac_sha256(salt, msg)
    return f"{TOKEN_PREFIX}{kind_clean}_{digest[:16].hex()}"


def is_token(value: str) -> bool:
    """Return True if ``value`` looks like a tokenized PII string."""
    if not isinstance(value, str):
        return False
    return value.startswith(TOKEN_PREFIX)


__all__ = [
    "PIITokenizationError",
    "KNOWN_KINDS",
    "PII_TENANT_SALT_NAME",
    "TOKEN_PREFIX",
    "hmac_sha256",
    "is_token",
    "tokenize",
]
