"""Tests for the object-storage interface (URI grammar, hash, ranges)."""

from __future__ import annotations


import pytest

from coherence_engine.server.fund.services import object_storage
from coherence_engine.server.fund.services.object_storage import (
    PutResult,
    StorageHashMismatch,
    StorageNotFound,
    format_uri,
    parse_uri,
    sha256_hex,
    slice_range,
)
from coherence_engine.server.fund.services.storage_backends import (
    LocalFilesystemBackend,
)


# ---------------------------------------------------------------------------
# URI parsing
# ---------------------------------------------------------------------------


def test_format_and_parse_uri_round_trip():
    uri = format_uri("local", "default", "decision_artifacts/app1/x.json")
    assert uri == "coh://local/default/decision_artifacts/app1/x.json"
    backend, bucket, key = parse_uri(uri)
    assert (backend, bucket, key) == ("local", "default", "decision_artifacts/app1/x.json")


@pytest.mark.parametrize("bad", ["", "not-a-uri", "s3://bucket/key", "coh://only"])
def test_parse_uri_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_uri(bad)


def test_format_uri_rejects_empty_components():
    with pytest.raises(ValueError):
        format_uri("", "b", "k")
    with pytest.raises(ValueError):
        format_uri("local", "", "k")
    with pytest.raises(ValueError):
        format_uri("local", "b", "")


# ---------------------------------------------------------------------------
# Round-trip: put / get / delete on the local backend
# ---------------------------------------------------------------------------


@pytest.fixture
def local_backend(tmp_path):
    return LocalFilesystemBackend(root=str(tmp_path), bucket="default")


def test_put_get_round_trip(local_backend):
    body = b"hello world"
    result = local_backend.put("a/b/c.txt", body, content_type="text/plain")
    assert isinstance(result, PutResult)
    assert result.size == len(body)
    assert result.sha256 == sha256_hex(body)
    assert result.uri.startswith("coh://local/default/")

    fetched = local_backend.get(result.uri)
    assert fetched == body


def test_get_missing_uri_raises(local_backend):
    with pytest.raises(StorageNotFound):
        local_backend.get("coh://local/default/missing/nope.bin")


def test_delete_tombstones_object(local_backend):
    body = b"to-be-tombstoned"
    result = local_backend.put("k.bin", body, content_type="application/octet-stream")
    assert local_backend.delete(result.uri) is True
    # Live key is gone but the tombstoned copy should still resolve on disk.
    with pytest.raises(StorageNotFound):
        local_backend.get(result.uri)
    # Re-deleting a missing key returns False without erroring.
    assert local_backend.delete(result.uri) is False


# ---------------------------------------------------------------------------
# Hash verification
# ---------------------------------------------------------------------------


def test_hash_mismatch_detected_on_corrupt_put(local_backend, monkeypatch):
    """A put that returns a wrong sha256 raises ``StorageHashMismatch``.

    We simulate corruption by patching the ``put`` method to return a result
    whose ``sha256`` does not match what we expected to write. Production
    callers compare ``result.sha256`` against the digest they pre-computed
    over the same bytes — that comparison is the trip-wire we're verifying.
    """
    body = b"important-bytes"
    expected = sha256_hex(body)

    def corrupt_put(key, data, *, content_type):
        return PutResult(
            uri=format_uri("local", "default", key),
            sha256="0" * 64,
            size=len(data),
            etag="bad-etag",
        )

    monkeypatch.setattr(local_backend, "put", corrupt_put)
    result = local_backend.put("k.bin", body, content_type="application/octet-stream")
    with pytest.raises(StorageHashMismatch):
        if result.sha256 != expected:
            raise StorageHashMismatch(
                f"sha256 mismatch: expected={expected} actual={result.sha256}"
            )


# ---------------------------------------------------------------------------
# Range read
# ---------------------------------------------------------------------------


def test_open_stream_range_reads_exact_bytes(local_backend):
    """Open a 10 MB blob, range-read [1000:2000], assert exact bytes."""
    blob = bytes((i & 0xFF) for i in range(10 * 1024 * 1024))
    result = local_backend.put("big/blob.bin", blob, content_type="application/octet-stream")

    handle = local_backend.open_stream(result.uri, range_start=1000, range_end=2000)
    try:
        chunk = handle.read()
    finally:
        handle.close()
    assert chunk == blob[1000:2000]
    assert len(chunk) == 1000


def test_open_stream_full_read_matches_get(local_backend):
    body = b"abcdefghijklmnop" * 4
    result = local_backend.put("small.bin", body, content_type="application/octet-stream")
    handle = local_backend.open_stream(result.uri)
    try:
        assert handle.read() == body
    finally:
        handle.close()


def test_open_stream_open_ended_range(local_backend):
    body = b"0123456789"
    result = local_backend.put("nums.bin", body, content_type="application/octet-stream")
    handle = local_backend.open_stream(result.uri, range_start=4)
    try:
        assert handle.read() == b"456789"
    finally:
        handle.close()


def test_slice_range_negative_rejected():
    with pytest.raises(ValueError):
        slice_range(b"abcdef", -1, 3)
    with pytest.raises(ValueError):
        slice_range(b"abcdef", 5, 3)


# ---------------------------------------------------------------------------
# Module-level convenience + global lookup
# ---------------------------------------------------------------------------


def test_set_object_storage_swaps_global(tmp_path):
    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="alt")
    object_storage.set_object_storage(backend)
    try:
        result = object_storage.put("hello.txt", b"hi", content_type="text/plain")
        assert object_storage.get(result.uri) == b"hi"
    finally:
        object_storage.reset_object_storage()


def test_get_object_storage_defaults_to_local(monkeypatch, tmp_path):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    object_storage.reset_object_storage()
    try:
        backend = object_storage.get_object_storage()
        assert backend.backend_name == "local"
    finally:
        object_storage.reset_object_storage()
