# Browser-mode founder interview (prompt 39)

## Why this surface exists

Wave 11's interview ingestion fans out across two paths: the Twilio
phone path (prompt 38) and this in-page WebRTC path. Both terminate
in the same outbox event — `interview_session_completed` — so
downstream scoring is mode-agnostic. A founder who cannot or
prefers not to take a phone call can complete the same interview in
the founder portal with a working microphone.

## End-to-end flow

```
+---------------------+        +--------------------------+
|  Founder browser    |        |  Fund backend (FastAPI)  |
|---------------------|        |--------------------------|
|  MediaRecorder      |        |  /interviews/...         |
|  (audio/webm; opus) |        |    chunks:initiate       |
|         |           |        |    chunks:complete       |
|         | onChunk   |        |    :finalize             |
|         v           |        |--------------------------|
|  chunked_upload.ts  |        |  voice_intake.py         |
|    initiate ------- | -----> |    start_browser_session |
|    PUT (signed URL) | =====> |    stitch_chunks         |
|    complete ------- | -----> |    finalize_browser_..   |
|                     |        |                          |
+---------------------+        +-------------+------------+
                                             |
                                             v
                                +------------+------------+
                                |  Object storage         |
                                |    interviews/<sid>/    |
                                |      chunk_00000.webm   |
                                |      chunk_00001.webm   |
                                |      ...                |
                                |      full.webm          |
                                +-------------------------+
                                             |
                                             v
                                +-------------------------+
                                |  EventOutbox            |
                                |    interview_session_   |
                                |      completed (browser)|
                                +-------------------------+
```

## Wire format

Every chunk is `audio/webm; codecs=opus` produced by a single
`MediaRecorder` instance with a 5-second timeslice. Because every
chunk shares codec parameters, server-side stitching is
codec-copy concat (`ffmpeg -f concat -safe 0 -i list.txt -c copy
out.webm`) — no re-encoding, no quality loss, deterministic output.

If a chunk's recorded codec parameters drift (e.g. browser switches
encoder mid-stream), the concat fails fast rather than silently
re-encoding. This is intentional: a drift indicates a client-side
bug we want surfaced, not papered over.

## Sequence ordering contract

The server is the source of truth for `seq` ordering.
`chunks:initiate` consults the database for the next-expected `seq`
on the session and rejects any client proposal that does not match.
This means:

* A buggy client that retries `seq=0` after committing it (replay)
  gets a `409 SEQ_OUT_OF_ORDER`.
* A client that skips ahead (out-of-order or lossy queue) gets the
  same `409`.
* A client that crashes mid-session can resume with the next
  expected `seq` simply by asking again.

## Idempotency

* `chunks:complete` is idempotent on `(chunk_id)`. A re-call against
  an already-completed chunk returns the existing envelope.
* `:finalize` is idempotent on `session.status == "completed"`. A
  second call returns a 200 with `idempotent: true` and points at
  the previously-stitched `full.webm`.

## Storage layout

```
interviews/<session_id>/chunk_00000.webm
interviews/<session_id>/chunk_00001.webm
...
interviews/<session_id>/full.webm
```

Chunk objects are not deleted after stitching — they remain as
forensic evidence in case the stitched artifact is later contested
(e.g. an integrity check claims drift).

## Failure modes

| Failure                                | Server response          | Client behavior                |
|----------------------------------------|--------------------------|--------------------------------|
| Microphone permission denied           | n/a (browser-only)       | `WebRtcRecorderError` surfaced |
| `seq` proposal does not match expected | 409 `SEQ_OUT_OF_ORDER`   | retry with refreshed `seq`     |
| Chunk not present at signed URL        | 409 `CHUNK_NOT_FOUND`    | re-PUT bytes, re-call complete |
| Chunk size exceeds `_CHUNK_MAX_BYTES`  | 422 `VALIDATION_ERROR`   | abort session                  |
| ffmpeg unavailable on server           | 422 `FINALIZE_FAILED`    | escalate to ops                |
| ffmpeg returns non-zero                | 422 `FINALIZE_FAILED`    | escalate to ops                |
| Hash drift between PUT bytes and read  | 500 `STORAGE_HASH_DRIFT` | escalate to ops                |

## What this surface does NOT do

* It does not proxy chunk bytes through the backend — uploads go
  direct to object storage via signed URLs minted by `:initiate`.
* It does not trust client-supplied `seq` numbers without server-
  side monotonicity verification.
* It does not silently ignore ffmpeg non-zero return codes;
  finalize fails surfaced 4xx to the caller.
* It does not delete chunk objects on success.

## Configuration

| Env var          | Default | Purpose                          |
|------------------|---------|----------------------------------|
| `FFMPEG_BINARY`  | `ffmpeg`| Path to the ffmpeg binary used for `stitch_chunks`. Tests inject a deterministic shim. |
