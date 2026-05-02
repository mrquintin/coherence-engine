"""Concrete object-storage backends.

Three implementations live here:

* :class:`LocalFilesystemBackend` — default, CI-safe, zero-dep. Atomic writes
  via tempfile + rename + fsync; range reads via ``file.seek``.
* :class:`S3Backend` — boto3-backed. ``boto3`` is imported lazily inside the
  class so that selecting any other backend never needs the dependency.
* :class:`SupabaseStorageBackend` — httpx-backed. ``httpx`` is imported lazily
  for the same reason. Supports public-bucket downloads and signed-URL minting.

Every backend honors the same contract defined in :mod:`object_storage`:
``put`` returns a :class:`PutResult` whose sha256 is recomputed by the backend
on the bytes actually persisted; reads are range-requestable; and
:meth:`delete` is soft (tombstone copy + remove of the live key) — production
hard-deletes go through a separate ``--purge`` admin path.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import IO, Optional

from coherence_engine.server.fund.services.object_storage import (
    PutResult,
    StorageBackendError,
    StorageNotFound,
    format_uri,
    parse_uri,
    sha256_hex,
)


# ---------------------------------------------------------------------------
# Local filesystem
# ---------------------------------------------------------------------------


class LocalFilesystemBackend:
    """Default backend. Stores objects under ``<root>/<bucket>/<key>``.

    ``put`` is atomic: it writes to a sibling tempfile, ``fsync``s, then
    ``os.replace``s onto the final path. This guarantees the destination
    either contains the full new contents or the previous contents — never a
    truncated write. Tombstones live under ``<root>/<bucket>/.tombstone/<key>``.
    """

    backend_name = "local"

    def __init__(self, *, root: str, bucket: str = "default") -> None:
        self._root = Path(root).resolve()
        self.bucket = bucket
        (self._root / self.bucket).mkdir(parents=True, exist_ok=True)
        (self._root / self.bucket / ".tombstone").mkdir(parents=True, exist_ok=True)

    # ---- path helpers -----------------------------------------------------

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise StorageBackendError(f"unsafe key: {key!r}")
        return self._root / self.bucket / key

    def _resolve_from_uri(self, uri: str) -> Path:
        backend, bucket, key = parse_uri(uri)
        if backend != self.backend_name:
            raise StorageBackendError(
                f"backend mismatch: uri={backend} backend={self.backend_name}"
            )
        if bucket != self.bucket:
            raise StorageBackendError(
                f"bucket mismatch: uri={bucket} backend={self.bucket}"
            )
        return self._resolve(key)

    # ---- public api -------------------------------------------------------

    def put(self, key: str, data: bytes, *, content_type: str) -> PutResult:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write.
        fd, tmp = tempfile.mkstemp(prefix=".put.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        # Re-read what we actually wrote so the digest reflects on-disk bytes.
        with path.open("rb") as fh:
            persisted = fh.read()
        digest = sha256_hex(persisted)
        size = len(persisted)
        return PutResult(
            uri=format_uri(self.backend_name, self.bucket, key),
            sha256=digest,
            size=size,
            etag=digest,
        )

    def get(self, uri: str) -> bytes:
        path = self._resolve_from_uri(uri)
        if not path.exists():
            raise StorageNotFound(uri)
        with path.open("rb") as fh:
            return fh.read()

    def open_stream(
        self,
        uri: str,
        *,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
    ) -> IO[bytes]:
        path = self._resolve_from_uri(uri)
        if not path.exists():
            raise StorageNotFound(uri)
        fh = path.open("rb")
        if range_start is None and range_end is None:
            return fh
        try:
            start = 0 if range_start is None else int(range_start)
            fh.seek(start)
            if range_end is None:
                return fh
            length = int(range_end) - start
            if length < 0:
                raise ValueError(f"invalid range [{start}, {range_end})")
            buf = fh.read(length)
        finally:
            if range_end is not None:
                fh.close()
        return io.BytesIO(buf)

    def delete(self, uri: str) -> bool:
        path = self._resolve_from_uri(uri)
        if not path.exists():
            return False
        _, _, key = parse_uri(uri)
        tomb = self._root / self.bucket / ".tombstone" / key
        tomb.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(tomb))
        return True


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


class S3Backend:
    """boto3-backed S3 backend. ``boto3`` is imported on first use.

    Reads use ``Range: bytes=A-B`` (HTTP-range, inclusive end). The internal
    surface still takes Python-slice ``[start, end)`` and converts on the way
    out so the ObjectStorage protocol stays consistent across backends.
    """

    backend_name = "s3"

    def __init__(self, *, bucket: str, region: Optional[str] = None) -> None:
        self.bucket = bucket
        self._region = region or os.environ.get("AWS_REGION") or None
        self._client = None  # built lazily by _client_for

    def _client_for(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except ImportError as exc:  # pragma: no cover - dep-optional path
            raise StorageBackendError("boto3 not installed; pip install boto3") from exc
        kwargs = {}
        if self._region:
            kwargs["region_name"] = self._region
        self._client = boto3.client("s3", **kwargs)
        return self._client

    def _key_from_uri(self, uri: str) -> str:
        backend, bucket, key = parse_uri(uri)
        if backend != self.backend_name:
            raise StorageBackendError(
                f"backend mismatch: uri={backend} backend={self.backend_name}"
            )
        if bucket != self.bucket:
            raise StorageBackendError(
                f"bucket mismatch: uri={bucket} backend={self.bucket}"
            )
        return key

    def put(self, key: str, data: bytes, *, content_type: str) -> PutResult:
        client = self._client_for()
        try:
            resp = client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as exc:  # pragma: no cover - exercised via moto
            raise StorageBackendError(f"s3 put failed: {exc}") from exc
        digest = sha256_hex(data)
        etag = str(resp.get("ETag", "")).strip('"')
        return PutResult(
            uri=format_uri(self.backend_name, self.bucket, key),
            sha256=digest,
            size=len(data),
            etag=etag or digest,
        )

    def get(self, uri: str) -> bytes:
        key = self._key_from_uri(uri)
        client = self._client_for()
        try:
            resp = client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_s3_not_found(exc):
                raise StorageNotFound(uri) from exc
            raise StorageBackendError(f"s3 get failed: {exc}") from exc
        return resp["Body"].read()

    def open_stream(
        self,
        uri: str,
        *,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
    ) -> IO[bytes]:
        key = self._key_from_uri(uri)
        client = self._client_for()
        kwargs = {"Bucket": self.bucket, "Key": key}
        if range_start is not None or range_end is not None:
            start = 0 if range_start is None else int(range_start)
            # HTTP Range is inclusive on the upper bound; Python slices are
            # exclusive. Convert by subtracting 1 from range_end.
            if range_end is None:
                kwargs["Range"] = f"bytes={start}-"
            else:
                end = int(range_end) - 1
                if end < start:
                    raise ValueError(f"invalid range [{start}, {range_end})")
                kwargs["Range"] = f"bytes={start}-{end}"
        try:
            resp = client.get_object(**kwargs)
        except Exception as exc:
            if _is_s3_not_found(exc):
                raise StorageNotFound(uri) from exc
            raise StorageBackendError(f"s3 get_stream failed: {exc}") from exc
        # boto3's StreamingBody is iterable; wrap to a plain BytesIO so callers
        # can treat it as a uniform binary file-like and ``.read()`` it.
        body = resp["Body"].read()
        return io.BytesIO(body)

    def delete(self, uri: str) -> bool:
        key = self._key_from_uri(uri)
        client = self._client_for()
        # Soft delete: copy to tombstone/<key>, then delete the live object.
        tomb_key = f"tombstone/{key}"
        try:
            try:
                client.head_object(Bucket=self.bucket, Key=key)
            except Exception as exc:
                if _is_s3_not_found(exc):
                    return False
                raise
            client.copy_object(
                Bucket=self.bucket,
                Key=tomb_key,
                CopySource={"Bucket": self.bucket, "Key": key},
            )
            client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageBackendError(f"s3 delete failed: {exc}") from exc
        return True

    def signed_url(self, uri: str, *, expires_in: int = 300) -> str:
        """Mint a presigned GET URL for time-limited public reads."""
        key = self._key_from_uri(uri)
        client = self._client_for()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )


def _is_s3_not_found(exc: BaseException) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return code in {"NoSuchKey", "404", "NotFound"}


# ---------------------------------------------------------------------------
# Supabase Storage
# ---------------------------------------------------------------------------


class SupabaseStorageBackend:
    """Supabase Storage backend over the REST API.

    Uses the service-role JWT for server-side calls (bypasses RLS by design).
    Supports public-bucket reads and signed-URL minting. ``httpx`` is
    imported lazily.
    """

    backend_name = "supabase"

    def __init__(
        self,
        *,
        bucket: str,
        url: Optional[str] = None,
        service_role_key: Optional[str] = None,
        client=None,  # injectable for tests
    ) -> None:
        self.bucket = bucket
        self._url = (url or os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self._service_role_key = service_role_key or os.environ.get(
            "SUPABASE_SERVICE_ROLE_KEY"
        )
        self._client = client  # if injected, used directly

    def _http(self):
        if self._client is not None:
            return self._client
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover - dep-optional path
            raise StorageBackendError("httpx not installed; pip install httpx") from exc
        if not self._url:
            raise StorageBackendError("SUPABASE_URL is required for the supabase backend")
        if not self._service_role_key:
            raise StorageBackendError(
                "SUPABASE_SERVICE_ROLE_KEY is required for the supabase backend"
            )
        return httpx.Client(timeout=30.0)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._service_role_key or ''}",
            "apikey": self._service_role_key or "",
        }

    def _object_url(self, key: str) -> str:
        # Supabase Storage REST: POST /storage/v1/object/<bucket>/<key>
        return f"{self._url}/storage/v1/object/{self.bucket}/{urllib.parse.quote(key)}"

    def _signed_url(self, key: str) -> str:
        return f"{self._url}/storage/v1/object/sign/{self.bucket}/{urllib.parse.quote(key)}"

    def _key_from_uri(self, uri: str) -> str:
        backend, bucket, key = parse_uri(uri)
        if backend != self.backend_name:
            raise StorageBackendError(
                f"backend mismatch: uri={backend} backend={self.backend_name}"
            )
        if bucket != self.bucket:
            raise StorageBackendError(
                f"bucket mismatch: uri={bucket} backend={self.bucket}"
            )
        return key

    def put(self, key: str, data: bytes, *, content_type: str) -> PutResult:
        client = self._http()
        headers = {**self._headers(), "Content-Type": content_type, "x-upsert": "true"}
        try:
            resp = client.post(self._object_url(key), content=data, headers=headers)
        except Exception as exc:  # pragma: no cover - transport
            raise StorageBackendError(f"supabase put failed: {exc}") from exc
        if resp.status_code >= 300:
            raise StorageBackendError(
                f"supabase put failed: {resp.status_code} {resp.text}"
            )
        digest = sha256_hex(data)
        try:
            etag = (resp.headers.get("ETag") or resp.json().get("Key") or digest)
        except Exception:
            etag = digest
        return PutResult(
            uri=format_uri(self.backend_name, self.bucket, key),
            sha256=digest,
            size=len(data),
            etag=str(etag).strip('"'),
        )

    def get(self, uri: str) -> bytes:
        key = self._key_from_uri(uri)
        client = self._http()
        try:
            resp = client.get(self._object_url(key), headers=self._headers())
        except Exception as exc:  # pragma: no cover
            raise StorageBackendError(f"supabase get failed: {exc}") from exc
        if resp.status_code == 404:
            raise StorageNotFound(uri)
        if resp.status_code >= 300:
            raise StorageBackendError(
                f"supabase get failed: {resp.status_code} {resp.text}"
            )
        return resp.content

    def open_stream(
        self,
        uri: str,
        *,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
    ) -> IO[bytes]:
        key = self._key_from_uri(uri)
        client = self._http()
        headers = dict(self._headers())
        if range_start is not None or range_end is not None:
            start = 0 if range_start is None else int(range_start)
            if range_end is None:
                headers["Range"] = f"bytes={start}-"
            else:
                end = int(range_end) - 1
                if end < start:
                    raise ValueError(f"invalid range [{start}, {range_end})")
                headers["Range"] = f"bytes={start}-{end}"
        try:
            resp = client.get(self._object_url(key), headers=headers)
        except Exception as exc:  # pragma: no cover
            raise StorageBackendError(f"supabase get_stream failed: {exc}") from exc
        if resp.status_code == 404:
            raise StorageNotFound(uri)
        if resp.status_code >= 300 and resp.status_code != 206:
            raise StorageBackendError(
                f"supabase get_stream failed: {resp.status_code} {resp.text}"
            )
        return io.BytesIO(resp.content)

    def delete(self, uri: str) -> bool:
        key = self._key_from_uri(uri)
        client = self._http()
        # Soft delete = move to tombstone path. Supabase Storage exposes a
        # ``move`` REST verb at /storage/v1/object/move which keeps the
        # operation atomic on the server side.
        move_url = f"{self._url}/storage/v1/object/move"
        body = {
            "bucketId": self.bucket,
            "sourceKey": key,
            "destinationKey": f"tombstone/{key}",
        }
        try:
            resp = client.post(move_url, json=body, headers=self._headers())
        except Exception as exc:  # pragma: no cover
            raise StorageBackendError(f"supabase delete failed: {exc}") from exc
        if resp.status_code == 404:
            return False
        if resp.status_code >= 300:
            raise StorageBackendError(
                f"supabase delete failed: {resp.status_code} {resp.text}"
            )
        return True

    def signed_url(self, uri: str, *, expires_in: int = 300) -> str:
        """Mint a Supabase signed URL for a private object."""
        key = self._key_from_uri(uri)
        client = self._http()
        try:
            resp = client.post(
                self._signed_url(key),
                json={"expiresIn": int(expires_in)},
                headers=self._headers(),
            )
        except Exception as exc:  # pragma: no cover
            raise StorageBackendError(f"supabase signed_url failed: {exc}") from exc
        if resp.status_code >= 300:
            raise StorageBackendError(
                f"supabase signed_url failed: {resp.status_code} {resp.text}"
            )
        try:
            body = resp.json()
        except Exception as exc:
            raise StorageBackendError(f"supabase signed_url malformed: {exc}") from exc
        signed = body.get("signedURL") or body.get("signedUrl") or body.get("url")
        if not signed:
            raise StorageBackendError(f"supabase signed_url missing url: {body!r}")
        if signed.startswith("/"):
            return f"{self._url}{signed}"
        return signed
