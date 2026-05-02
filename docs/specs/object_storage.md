# Object Storage (transcripts, argument graphs, decision artifacts)

> **Status:** prompt 29 of 70 (Wave 9, independent).
> **Code:** `server/fund/services/object_storage.py`,
> `server/fund/services/storage_backends.py`.
> **Tests:** `tests/test_object_storage.py`, `tests/test_storage_backends.py`.

This adapter is the single chokepoint for blob persistence in the fund
backend. Every code path that produces or consumes a transcript, an argument
graph (propositions / relations), or a decision artifact body goes through
the `ObjectStorage` interface — not the database, not the filesystem, not a
provider SDK directly. Postgres holds metadata and indexed columns; the
adapter holds bodies.

## URI grammar

All stored objects are addressed by a canonical URI:

    coh://<backend>/<bucket>/<key>

* `<backend>` ∈ `local` | `s3` | `supabase`
* `<bucket>` is backend-defined (subdir for `local`, S3 bucket name, Supabase
  storage bucket name)
* `<key>` is a forward-slash-delimited path. Keys must not contain `..` and
  must not start with `/`.

Example: `coh://s3/coherence-prod/transcripts/app_abc/sess_123.json`.

The `coh://` prefix is intentional: it makes URIs stored in Postgres easily
distinguishable from legacy `db://` placeholders in
`Application.transcript_uri`, and lets us migrate backends without changing
the column.

## Contract

* **Hash discipline.** Every `put` returns a `PutResult(uri, sha256, size,
  etag)`. The `sha256` is recomputed by the backend on the bytes actually
  persisted. Callers compute the digest over the bytes they intended to send
  and compare; on mismatch they raise `StorageHashMismatch` and refuse to
  persist the URI. Decision-artifact callers also recompute the hash on read
  before trusting the body. **Client-supplied hashes are never trusted for
  high-value artifacts.**
* **Range-readable.** `open_stream(uri, range_start=A, range_end=B)` returns
  a binary file-like over `[A, B)` (Python-slice semantics). The local
  backend implements this with `seek + read`; the S3 and Supabase backends
  translate to `Range: bytes=A-(B-1)` (HTTP range is inclusive on the upper
  bound). Workers stream large transcripts without buffering.
* **Soft delete.** `delete(uri)` is a tombstone: the live object is moved to
  a `tombstone/<key>` prefix and the live key is removed. Hard deletion is a
  separate `--purge` admin verb (not exposed through this interface). Soft
  delete preserves audit reconstructability while still removing the object
  from any list / signed-URL handouts the founder portal might serve.

## Lifecycle

| Phase     | Action                                                                          |
|-----------|---------------------------------------------------------------------------------|
| Live      | `put` writes under `<bucket>/<key>`; the URI is the authoritative reference.    |
| Tombstone | `delete(uri)` moves the object to `tombstone/<key>`. Live key returns 404.      |
| Purge     | An admin job (`coherence-engine storage purge`) hard-deletes tombstoned blobs after the retention window expires. |

Tombstones are **not** rehydrated by `get(uri)` — once an object is
tombstoned, callers see `StorageNotFound`. Recovery is operator-driven and
runs out-of-band (copy back from `tombstone/<key>` to `<key>`).

## Retention defaults

* **Transcripts:** 90 days. After tombstone, the purge job hard-deletes.
* **Argument propositions / relations:** 90 days alongside their transcript.
* **Decision artifact bodies:** indefinite. Decision artifacts are the legal
  record of every fund decision and must remain reachable for the lifetime of
  the fund. The DB row in `fund_argument_artifacts` is the authoritative
  copy; the storage copy is the streamable copy.

## Backends

### LocalFilesystemBackend (default; CI-safe)

Atomic writes via `tempfile.mkstemp + fsync + os.replace`. Tombstones live
under `<root>/<bucket>/.tombstone/<key>`. No external dependencies.

Configuration:

* `STORAGE_BACKEND=local` (default)
* `LOCAL_STORAGE_ROOT=./var/object_storage` (default)
* `LOCAL_STORAGE_BUCKET=default` (default)

### S3Backend

`boto3` is **lazily imported** — selecting any other backend never requires
the dependency. Reads use HTTP `Range` headers; signed URLs are minted via
`generate_presigned_url`. Soft delete is implemented as `copy_object` to
`tombstone/<key>` followed by `delete_object` on the live key.

Configuration:

* `STORAGE_BACKEND=s3`
* `S3_BUCKET=<bucket>`
* `AWS_REGION=<region>` (or AWS_DEFAULT_REGION)
* AWS credentials via standard boto3 chain.

### SupabaseStorageBackend

`httpx` is **lazily imported**. Calls are made with the service-role JWT,
which bypasses RLS by design (server-side only — never expose this key to
the founder-portal browser). The signed-URL flow returns an absolute URL
suitable for handing to the Next.js app.

Configuration:

* `STORAGE_BACKEND=supabase`
* `SUPABASE_STORAGE_BUCKET=<bucket>`
* `SUPABASE_URL=<https://...supabase.co>`
* `SUPABASE_SERVICE_ROLE_KEY=<jwt>`

## Founder-portal signed-URL flow

The Next.js founder-portal app must **never** receive the Supabase service-
role key. The fund backend mints a short-lived signed URL on demand:

1. Founder hits the portal: `GET /portal/applications/:id/transcript`.
2. Portal calls the fund backend with a Supabase user JWT.
3. Backend validates the JWT, confirms the founder owns the application via
   `applications.founder_user_id`, and calls
   `SupabaseStorageBackend.signed_url(uri, expires_in=300)`.
4. Backend returns the URL to the portal; the portal redirects (302) the
   browser to it.
5. Supabase Storage serves the bytes directly — the fund backend is not in
   the data path.

The same mechanism works for S3 (presigned `get_object` URL) and is a no-op
for the local backend (only the operator console reads from local storage).

## Prohibitions

* **Do not import boto3 or httpx at module top-level.** Both must be
  lazy-imported inside the corresponding backend's `_client_for` /
  `_http` method. CI runs the local backend without either dep installed.
* **Do not trust client-supplied content hashes.** Every server-side caller
  recomputes the digest from the bytes it sent; high-value artifacts
  (decision bodies) recompute on read as well.
* **Do not hard-delete in production paths.** All synchronous `delete(uri)`
  calls go through the soft tombstone. Hard deletion is a separate
  `--purge` admin verb that runs on a retention-expiry schedule.
* **Do not embed bucket / region / credentials in URIs.** The canonical
  `coh://<backend>/<bucket>/<key>` URI is provider-agnostic; backend
  selection lives in environment configuration.
