/**
 * Chunked upload pipeline for browser-mode founder interviews (prompt 39).
 *
 * Three-step flow per chunk, mirroring the deck-upload pattern in
 * ``./upload.ts`` but parameterized for ``audio/webm`` chunks driven
 * by the {@link WebRtcRecorder}:
 *
 *   1. POST ``/interviews/{session_id}/chunks:initiate`` with
 *      ``{ seq, size_bytes }`` — backend mints a signed URL keyed
 *      ``interviews/<session>/chunk_<seq>.webm`` and returns the
 *      ``chunk_id``, ``upload_url``, and storage URI.
 *   2. PUT the chunk bytes directly to ``upload_url``.
 *   3. POST ``/interviews/{session_id}/chunks:complete`` with the
 *      ``chunk_id`` so the backend can verify size + SHA-256 and
 *      flip the chunk row to ``"completed"``.
 *
 * On transient failure each chunk retries with exponential backoff.
 * The pipeline preserves seq ordering by serializing chunk uploads —
 * an out-of-order PUT would be rejected by the server's
 * monotonic-seq guard, so parallelism here would only race itself
 * into a 409.
 */

export interface InitiateChunkInput {
  sessionId: string;
  seq: number;
  sizeBytes: number;
}

export interface InitiateChunkResult {
  chunk_id: string;
  session_id: string;
  seq: number;
  upload_url: string;
  headers: Record<string, string>;
  expires_at: string;
  key: string;
  uri: string;
  max_bytes: number;
}

export interface CompleteChunkResult {
  chunk_id: string;
  seq: number;
  uri: string;
  size_bytes: number;
  sha256: string;
  status: "completed";
}

export interface FinalizeSessionResult {
  session_id: string;
  status: "completed";
  full_uri?: string;
  full_sha256?: string;
  chunk_count?: number;
  event_id?: string;
  idempotent?: boolean;
}

export class ChunkUploadError extends Error {
  readonly phase: "initiate" | "put" | "complete" | "finalize";
  readonly seq?: number;
  constructor(phase: "initiate" | "put" | "complete" | "finalize", message: string, seq?: number) {
    super(message);
    this.name = "ChunkUploadError";
    this.phase = phase;
    this.seq = seq;
  }
}

export interface ChunkedUploadDeps {
  /** Override for tests. Defaults to global ``fetch``. */
  fetchImpl?: typeof fetch;
  /** Sleep helper, swappable in tests for deterministic backoff. */
  sleep?: (ms: number) => Promise<void>;
  /** Max retries per phase. Defaults to 3. */
  maxRetries?: number;
}

const DEFAULT_RETRIES = 3;

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function withRetry<T>(
  phase: "initiate" | "put" | "complete" | "finalize",
  seq: number | undefined,
  maxRetries: number,
  sleep: (ms: number) => Promise<void>,
  attempt: () => Promise<T>,
): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i <= maxRetries; i += 1) {
    try {
      return await attempt();
    } catch (err) {
      lastErr = err;
      if (i === maxRetries) {
        break;
      }
      // Backoff: 100ms, 300ms, 900ms, ... — capped at 5s.
      const backoff = Math.min(100 * 3 ** i, 5_000);
      await sleep(backoff);
    }
  }
  throw new ChunkUploadError(
    phase,
    lastErr instanceof Error ? lastErr.message : String(lastErr),
    seq,
  );
}

export async function initiateChunk(
  input: InitiateChunkInput,
  deps: ChunkedUploadDeps = {},
): Promise<InitiateChunkResult> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const sleep = deps.sleep ?? defaultSleep;
  const maxRetries = deps.maxRetries ?? DEFAULT_RETRIES;
  return withRetry("initiate", input.seq, maxRetries, sleep, async () => {
    const res = await fetchImpl(
      `/api/v1/interviews/${encodeURIComponent(input.sessionId)}/chunks:initiate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seq: input.seq, size_bytes: input.sizeBytes }),
      },
    );
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`initiate failed (${res.status}): ${text}`);
    }
    const parsed = JSON.parse(text) as { data?: InitiateChunkResult };
    if (!parsed.data) {
      throw new Error("initiate envelope missing data");
    }
    return parsed.data;
  });
}

export async function putChunkBytes(
  signed_url: string,
  headers: Record<string, string>,
  blob: Blob,
  seq: number,
  deps: ChunkedUploadDeps = {},
): Promise<void> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const sleep = deps.sleep ?? defaultSleep;
  const maxRetries = deps.maxRetries ?? DEFAULT_RETRIES;
  await withRetry("put", seq, maxRetries, sleep, async () => {
    const res = await fetchImpl(signed_url, {
      method: "PUT",
      headers,
      body: blob,
    });
    if (!res.ok) {
      throw new Error(`PUT failed (${res.status})`);
    }
  });
}

export async function completeChunk(
  sessionId: string,
  chunkId: string,
  seq: number,
  deps: ChunkedUploadDeps = {},
): Promise<CompleteChunkResult> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const sleep = deps.sleep ?? defaultSleep;
  const maxRetries = deps.maxRetries ?? DEFAULT_RETRIES;
  return withRetry("complete", seq, maxRetries, sleep, async () => {
    const res = await fetchImpl(
      `/api/v1/interviews/${encodeURIComponent(sessionId)}/chunks:complete`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chunk_id: chunkId }),
      },
    );
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`complete failed (${res.status}): ${text}`);
    }
    const parsed = JSON.parse(text) as { data?: CompleteChunkResult };
    if (!parsed.data) {
      throw new Error("complete envelope missing data");
    }
    return parsed.data;
  });
}

export async function finalizeSession(
  sessionId: string,
  deps: ChunkedUploadDeps = {},
): Promise<FinalizeSessionResult> {
  const fetchImpl = deps.fetchImpl ?? fetch;
  const sleep = deps.sleep ?? defaultSleep;
  const maxRetries = deps.maxRetries ?? DEFAULT_RETRIES;
  return withRetry("finalize", undefined, maxRetries, sleep, async () => {
    const res = await fetchImpl(`/api/v1/interviews/${encodeURIComponent(sessionId)}:finalize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`finalize failed (${res.status}): ${text}`);
    }
    const parsed = JSON.parse(text) as { data?: FinalizeSessionResult };
    if (!parsed.data) {
      throw new Error("finalize envelope missing data");
    }
    return parsed.data;
  });
}

/**
 * Convenience: end-to-end uploader for a single chunk.
 *
 * The interview UI calls this from the recorder's ``onChunk``
 * callback. The function awaits each phase serially so that the
 * backend's monotonic-seq guard cannot be raced into rejecting an
 * out-of-order PUT.
 */
export async function uploadChunk(
  sessionId: string,
  chunk: { seq: number; blob: Blob },
  deps: ChunkedUploadDeps = {},
): Promise<CompleteChunkResult> {
  const initiated = await initiateChunk(
    { sessionId, seq: chunk.seq, sizeBytes: chunk.blob.size },
    deps,
  );
  await putChunkBytes(initiated.upload_url, initiated.headers, chunk.blob, chunk.seq, deps);
  return completeChunk(sessionId, initiated.chunk_id, chunk.seq, deps);
}
