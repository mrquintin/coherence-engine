"""Object-storage adapter (transcripts, argument graphs, decision artifacts).

This module defines the storage-agnostic interface used by every code path that
writes or reads large blobs (transcripts, argument propositions / relations,
decision artifact bodies). Concrete backends (local filesystem, S3, Supabase
Storage) live in :mod:`storage_backends`; this file owns the URI grammar, the
result + exception types, and the global lookup that selects a backend from
``STORAGE_BACKEND``.

URI format
----------

    coh://<backend>/<bucket>/<key>

``backend`` is one of ``local | s3 | supabase``. ``bucket`` and ``key`` are
backend-defined: for ``local`` the bucket maps to a sub-directory under
``LOCAL_STORAGE_ROOT``; for S3 it is the bucket name; for Supabase it is the
storage bucket name. Backends translate this canonical URI to their native
identifier internally.

Contract
--------

* Every :meth:`ObjectStorage.put` returns a :class:`PutResult` whose
  ``sha256`` is the hex-digest of the bytes the backend actually wrote. Callers
  compare against their pre-computed digest and raise
  :class:`StorageHashMismatch` on mismatch — never trust client-supplied
  digests for high-value artifacts (decision bodies in particular: the server
  recomputes on read).
* Reads are range-requestable. ``open_stream(uri, range_start=A, range_end=B)``
  returns a binary file-like over bytes ``[A:B)`` (Python-slice semantics:
  ``range_end`` is exclusive). This lets the worker stream a 10 MB transcript
  without buffering the whole blob in memory.
* :meth:`ObjectStorage.delete` is soft in production: it copies the object to a
  ``tombstone/`` prefix and removes the live key. Hard deletion lives behind a
  separate ``--purge`` admin verb (see ``docs/specs/object_storage.md``).
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from typing import IO, Optional, Protocol, Tuple

from coherence_engine.server.fund.observability.otel import (
    get_tracer,
    safe_set_attributes,
)


_TRACER = get_tracer("coherence_engine.fund.services.object_storage")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PutResult:
    """Canonical return value for every successful put."""

    uri: str
    sha256: str
    size: int
    etag: str


class StorageError(Exception):
    """Base class for object-storage failures."""


class StorageHashMismatch(StorageError):
    """Raised when bytes read back do not match the expected SHA-256.

    Production callers should treat this as data corruption: log the URI, fail
    the surrounding workflow stage, and let the workflow orchestrator retry
    against the same URI (idempotent reads).
    """


class StorageNotFound(StorageError):
    """Raised when the object addressed by a URI does not exist."""


class StorageBackendError(StorageError):
    """Raised for transport / provider errors that callers cannot recover from."""


# ---------------------------------------------------------------------------
# URI parsing
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"^coh://(?P<backend>[a-z0-9_]+)/(?P<bucket>[^/]+)/(?P<key>.+)$")


def format_uri(backend: str, bucket: str, key: str) -> str:
    """Build a canonical ``coh://`` URI."""
    if not backend or not bucket or not key:
        raise ValueError("backend, bucket, key must all be non-empty")
    return f"coh://{backend}/{bucket}/{key}"


def parse_uri(uri: str) -> Tuple[str, str, str]:
    """Parse a ``coh://`` URI into ``(backend, bucket, key)``.

    Raises :class:`ValueError` for malformed URIs.
    """
    if not isinstance(uri, str):
        raise ValueError(f"uri must be str, got {type(uri).__name__}")
    m = _URI_RE.match(uri)
    if not m:
        raise ValueError(f"not a valid coh:// uri: {uri!r}")
    return m.group("backend"), m.group("bucket"), m.group("key")


def sha256_hex(data: bytes) -> str:
    """Hex-encoded SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class ObjectStorage(Protocol):
    """The contract every backend implements."""

    backend_name: str
    bucket: str

    def put(self, key: str, data: bytes, *, content_type: str) -> PutResult:
        """Store ``data`` at ``key`` and return the canonical result."""

    def get(self, uri: str) -> bytes:
        """Return the full body for ``uri``."""

    def open_stream(
        self,
        uri: str,
        *,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
    ) -> IO[bytes]:
        """Return a binary file-like over ``[range_start, range_end)``."""

    def delete(self, uri: str) -> bool:
        """Soft-delete the object; return ``True`` if a live object was tombstoned."""


# ---------------------------------------------------------------------------
# Helpers for backends
# ---------------------------------------------------------------------------


def verify_sha256(data: bytes, expected: str, *, uri: str) -> None:
    """Raise :class:`StorageHashMismatch` when ``data`` does not match ``expected``."""
    actual = sha256_hex(data)
    if actual != expected:
        raise StorageHashMismatch(
            f"sha256 mismatch for {uri}: expected={expected} actual={actual}"
        )


def slice_range(data: bytes, range_start: Optional[int], range_end: Optional[int]) -> bytes:
    """Apply Python-slice ``[range_start:range_end]`` semantics with bounds-check."""
    if range_start is None and range_end is None:
        return data
    start = 0 if range_start is None else int(range_start)
    end = len(data) if range_end is None else int(range_end)
    if start < 0 or end < start:
        raise ValueError(f"invalid range [{start}, {end})")
    return data[start:end]


# ---------------------------------------------------------------------------
# Module-level convenience: lazy global backend
# ---------------------------------------------------------------------------


_BACKEND_LOCK = threading.Lock()
_BACKEND_INSTANCE: Optional[ObjectStorage] = None


def reset_object_storage() -> None:
    """Drop the cached backend (test-only)."""
    global _BACKEND_INSTANCE
    with _BACKEND_LOCK:
        _BACKEND_INSTANCE = None


def set_object_storage(backend: Optional[ObjectStorage]) -> None:
    """Inject a specific backend (test-only or worker bootstrap)."""
    global _BACKEND_INSTANCE
    with _BACKEND_LOCK:
        _BACKEND_INSTANCE = backend


def get_object_storage() -> ObjectStorage:
    """Return the process-wide :class:`ObjectStorage`, building one on first call.

    Selection order:

    * ``STORAGE_BACKEND`` env var: ``local`` (default), ``s3``, ``supabase``.
    * ``LOCAL_STORAGE_ROOT`` defaults to ``./var/object_storage`` for local.
    * ``S3_BUCKET`` / ``SUPABASE_STORAGE_BUCKET`` provide the bucket name for
      the matching backend.
    """
    global _BACKEND_INSTANCE
    with _BACKEND_LOCK:
        if _BACKEND_INSTANCE is not None:
            return _BACKEND_INSTANCE
        # Lazy-import to keep S3 / Supabase deps optional. The local backend
        # has no extras; importing the module is safe.
        from coherence_engine.server.fund.services import storage_backends

        backend_name = (os.environ.get("STORAGE_BACKEND") or "local").strip().lower()
        if backend_name == "local":
            root = os.environ.get("LOCAL_STORAGE_ROOT") or "./var/object_storage"
            bucket = os.environ.get("LOCAL_STORAGE_BUCKET") or "default"
            _BACKEND_INSTANCE = storage_backends.LocalFilesystemBackend(
                root=root, bucket=bucket
            )
        elif backend_name == "s3":
            bucket = os.environ.get("S3_BUCKET")
            if not bucket:
                raise StorageBackendError("S3_BUCKET must be set when STORAGE_BACKEND=s3")
            _BACKEND_INSTANCE = storage_backends.S3Backend(bucket=bucket)
        elif backend_name == "supabase":
            bucket = os.environ.get("SUPABASE_STORAGE_BUCKET")
            if not bucket:
                raise StorageBackendError(
                    "SUPABASE_STORAGE_BUCKET must be set when STORAGE_BACKEND=supabase"
                )
            _BACKEND_INSTANCE = storage_backends.SupabaseStorageBackend(bucket=bucket)
        else:
            raise StorageBackendError(f"unknown STORAGE_BACKEND={backend_name!r}")
        return _BACKEND_INSTANCE


# ---------------------------------------------------------------------------
# Module-level convenience wrappers (for callers that want functional style)
# ---------------------------------------------------------------------------


def put(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> PutResult:
    backend = get_object_storage()
    with _TRACER.start_as_current_span("object_storage.put") as span:
        safe_set_attributes(
            span,
            {
                "storage.backend": getattr(backend, "backend_name", "unknown"),
                "storage.bucket": getattr(backend, "bucket", "unknown"),
                "storage.key": key,
                "storage.size_bytes": len(data) if data is not None else 0,
                "storage.content_type": content_type,
            },
        )
        result = backend.put(key, data, content_type=content_type)
        safe_set_attributes(
            span,
            {
                "storage.uri": result.uri,
                "storage.sha256": result.sha256,
                "storage.etag": result.etag,
            },
        )
        return result


def get(uri: str) -> bytes:
    with _TRACER.start_as_current_span("object_storage.get") as span:
        safe_set_attributes(span, {"storage.uri": uri})
        body = get_object_storage().get(uri)
        safe_set_attributes(
            span, {"storage.size_bytes": len(body) if body is not None else 0}
        )
        return body


def open_stream(
    uri: str,
    *,
    range_start: Optional[int] = None,
    range_end: Optional[int] = None,
) -> IO[bytes]:
    with _TRACER.start_as_current_span("object_storage.open_stream") as span:
        safe_set_attributes(
            span,
            {
                "storage.uri": uri,
                "storage.range_start": range_start,
                "storage.range_end": range_end,
            },
        )
        return get_object_storage().open_stream(
            uri, range_start=range_start, range_end=range_end
        )


def delete(uri: str) -> bool:
    with _TRACER.start_as_current_span("object_storage.delete") as span:
        safe_set_attributes(span, {"storage.uri": uri})
        outcome = get_object_storage().delete(uri)
        safe_set_attributes(span, {"storage.deleted": bool(outcome)})
        return outcome
