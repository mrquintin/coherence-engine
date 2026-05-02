"""Tests for the concrete object-storage backends (local, S3, Supabase)."""

from __future__ import annotations


import pytest

from coherence_engine.server.fund.services.object_storage import (
    StorageBackendError,
    StorageNotFound,
    sha256_hex,
)
from coherence_engine.server.fund.services.storage_backends import (
    LocalFilesystemBackend,
    S3Backend,
    SupabaseStorageBackend,
)


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------


def test_local_backend_atomic_put_persists_full_bytes(tmp_path):
    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="default")
    body = b"x" * 4096
    result = backend.put("nested/key.bin", body, content_type="application/octet-stream")
    assert result.size == 4096
    assert result.sha256 == sha256_hex(body)
    assert backend.get(result.uri) == body


def test_local_backend_rejects_unsafe_keys(tmp_path):
    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="default")
    with pytest.raises(StorageBackendError):
        backend.put("../escape.bin", b"x", content_type="application/octet-stream")
    with pytest.raises(StorageBackendError):
        backend.put("/abs/path.bin", b"x", content_type="application/octet-stream")


def test_local_backend_bucket_mismatch_rejected(tmp_path):
    backend = LocalFilesystemBackend(root=str(tmp_path), bucket="alpha")
    other = LocalFilesystemBackend(root=str(tmp_path), bucket="beta")
    body = b"abc"
    result = other.put("k.bin", body, content_type="application/octet-stream")
    with pytest.raises(StorageBackendError):
        backend.get(result.uri)


# ---------------------------------------------------------------------------
# S3 backend (moto, optional)
# ---------------------------------------------------------------------------


try:  # pragma: no cover - dep-availability branch
    import moto  # type: ignore  # noqa: F401
    import boto3  # type: ignore  # noqa: F401

    _MOTO_AVAILABLE = True
except ImportError:
    _MOTO_AVAILABLE = False

s3 = pytest.mark.skipif(not _MOTO_AVAILABLE, reason="moto/boto3 not installed")


@pytest.fixture
def s3_backend(monkeypatch):
    from moto import mock_aws  # type: ignore
    import boto3  # type: ignore

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="ce-test-bucket")
        backend = S3Backend(bucket="ce-test-bucket", region="us-east-1")
        yield backend


@s3
def test_s3_put_and_get_round_trip(s3_backend):
    body = b"s3-round-trip"
    result = s3_backend.put("a/b.txt", body, content_type="text/plain")
    assert result.uri == "coh://s3/ce-test-bucket/a/b.txt"
    assert result.sha256 == sha256_hex(body)
    assert s3_backend.get(result.uri) == body


@s3
def test_s3_range_read(s3_backend):
    body = bytes(range(256)) * 100  # 25.6 KB
    result = s3_backend.put("blob.bin", body, content_type="application/octet-stream")
    handle = s3_backend.open_stream(result.uri, range_start=10, range_end=42)
    chunk = handle.read()
    assert chunk == body[10:42]


@s3
def test_s3_get_missing_raises_not_found(s3_backend):
    with pytest.raises(StorageNotFound):
        s3_backend.get("coh://s3/ce-test-bucket/nope.bin")


@s3
def test_s3_delete_tombstones(s3_backend):
    body = b"tomb"
    result = s3_backend.put("kill-me.bin", body, content_type="application/octet-stream")
    assert s3_backend.delete(result.uri) is True
    with pytest.raises(StorageNotFound):
        s3_backend.get(result.uri)
    # Tombstoned copy is reachable at the tombstone/ prefix.
    tomb_uri = "coh://s3/ce-test-bucket/tombstone/kill-me.bin"
    assert s3_backend.get(tomb_uri) == body


@s3
def test_s3_signed_url_returned(s3_backend):
    body = b"signed"
    result = s3_backend.put("signed.bin", body, content_type="application/octet-stream")
    url = s3_backend.signed_url(result.uri, expires_in=60)
    assert "ce-test-bucket" in url
    assert "signed.bin" in url
    assert "X-Amz-Expires=60" in url or "X-Amz-Expires=60&" in url + "&"


# ---------------------------------------------------------------------------
# Supabase backend (httpx stubbed)
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code=200, content=b"", json_body=None, headers=None):
        self.status_code = status_code
        self.content = content
        self._json = json_body
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _StubHttpClient:
    """Minimal httpx-shaped stub that records calls for assertion."""

    def __init__(self):
        self.calls = []
        self.responses = []  # FIFO queue of _StubResponse

    def _next(self):
        return self.responses.pop(0)

    def post(self, url, *, content=None, json=None, headers=None):
        self.calls.append(("POST", url, dict(headers or {}), content, json))
        return self._next()

    def get(self, url, *, headers=None):
        self.calls.append(("GET", url, dict(headers or {})))
        return self._next()


def test_supabase_put_sends_service_role_headers():
    client = _StubHttpClient()
    client.responses.append(_StubResponse(status_code=200, json_body={"Key": "transcripts/x.json"}))
    backend = SupabaseStorageBackend(
        bucket="evidence",
        url="https://example.supabase.co",
        service_role_key="srk-token",
        client=client,
    )
    body = b"hello"
    result = backend.put("transcripts/x.json", body, content_type="application/json")
    assert result.uri == "coh://supabase/evidence/transcripts/x.json"
    assert result.sha256 == sha256_hex(body)

    method, url, headers, content, json_body = client.calls[0]
    assert method == "POST"
    assert url == "https://example.supabase.co/storage/v1/object/evidence/transcripts/x.json"
    assert headers["Authorization"] == "Bearer srk-token"
    assert headers["apikey"] == "srk-token"
    assert headers["Content-Type"] == "application/json"
    assert headers["x-upsert"] == "true"
    assert content == body


def test_supabase_get_returns_body():
    client = _StubHttpClient()
    client.responses.append(_StubResponse(status_code=200, content=b"the-bytes"))
    backend = SupabaseStorageBackend(
        bucket="evidence",
        url="https://example.supabase.co",
        service_role_key="srk-token",
        client=client,
    )
    out = backend.get("coh://supabase/evidence/k.bin")
    assert out == b"the-bytes"


def test_supabase_get_missing_raises_not_found():
    client = _StubHttpClient()
    client.responses.append(_StubResponse(status_code=404, content=b"missing"))
    backend = SupabaseStorageBackend(
        bucket="evidence",
        url="https://example.supabase.co",
        service_role_key="srk-token",
        client=client,
    )
    with pytest.raises(StorageNotFound):
        backend.get("coh://supabase/evidence/missing.bin")


def test_supabase_open_stream_sends_range_header():
    client = _StubHttpClient()
    client.responses.append(_StubResponse(status_code=206, content=b"slice"))
    backend = SupabaseStorageBackend(
        bucket="evidence",
        url="https://example.supabase.co",
        service_role_key="srk-token",
        client=client,
    )
    handle = backend.open_stream(
        "coh://supabase/evidence/blob.bin", range_start=10, range_end=20
    )
    assert handle.read() == b"slice"
    method, url, headers = client.calls[0]
    assert method == "GET"
    # Range header is HTTP-inclusive on the upper bound; Python slice is exclusive.
    assert headers["Range"] == "bytes=10-19"


def test_supabase_signed_url_parses_response():
    client = _StubHttpClient()
    client.responses.append(
        _StubResponse(status_code=200, json_body={"signedURL": "/storage/v1/object/sign/x?token=abc"})
    )
    backend = SupabaseStorageBackend(
        bucket="evidence",
        url="https://example.supabase.co",
        service_role_key="srk-token",
        client=client,
    )
    url = backend.signed_url("coh://supabase/evidence/x.json", expires_in=120)
    assert url == "https://example.supabase.co/storage/v1/object/sign/x?token=abc"
    method, called_url, headers, content, json_body = client.calls[0]
    assert called_url == "https://example.supabase.co/storage/v1/object/sign/evidence/x.json"
    assert json_body == {"expiresIn": 120}


def test_supabase_delete_uses_move_endpoint():
    client = _StubHttpClient()
    client.responses.append(_StubResponse(status_code=200, json_body={"message": "ok"}))
    backend = SupabaseStorageBackend(
        bucket="evidence",
        url="https://example.supabase.co",
        service_role_key="srk-token",
        client=client,
    )
    assert backend.delete("coh://supabase/evidence/x.bin") is True
    method, url, headers, content, json_body = client.calls[0]
    assert url == "https://example.supabase.co/storage/v1/object/move"
    assert json_body == {
        "bucketId": "evidence",
        "sourceKey": "x.bin",
        "destinationKey": "tombstone/x.bin",
    }
