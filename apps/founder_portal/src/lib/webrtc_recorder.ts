/**
 * Browser-side audio capture for the in-page founder interview (prompt 39).
 *
 * Wraps the platform ``MediaRecorder`` API to emit fixed-length
 * ``audio/webm; codecs=opus`` chunks at a steady cadence. Each chunk
 * is handed to a caller-provided ``onChunk`` callback together with
 * its monotonic sequence number; the chunked-upload helper
 * (``./chunked_upload``) consumes that stream and ships each chunk
 * directly to object storage via a backend-minted signed URL.
 *
 * The recorder is intentionally framework-agnostic: no React, no
 * Zustand. The interview UI component owns lifecycle (start/stop)
 * and surfaces user-facing state.
 */

export const DEFAULT_CHUNK_INTERVAL_MS = 5_000;
export const DEFAULT_MIME_TYPE = "audio/webm; codecs=opus";

export interface RecorderChunk {
  /** Monotonic, gap-free sequence index — first chunk is 0. */
  seq: number;
  /** Wire-format blob (``audio/webm; codecs=opus``). */
  blob: Blob;
  /** Wall-clock ISO timestamp when the chunk was emitted by MediaRecorder. */
  emittedAt: string;
}

export interface WebRtcRecorderOptions {
  onChunk: (chunk: RecorderChunk) => void | Promise<void>;
  onError?: (error: Error) => void;
  onStop?: () => void;
  /** Override for tests. Defaults to ``navigator.mediaDevices.getUserMedia``. */
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  /** Override for tests. Defaults to the global ``MediaRecorder``. */
  recorderFactory?: (stream: MediaStream, opts: MediaRecorderOptions) => MediaRecorder;
  /** Chunk timeslice in ms. Defaults to {@link DEFAULT_CHUNK_INTERVAL_MS}. */
  chunkIntervalMs?: number;
  /** MIME type override. Defaults to {@link DEFAULT_MIME_TYPE}. */
  mimeType?: string;
}

export class WebRtcRecorderError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WebRtcRecorderError";
  }
}

/**
 * Stateful recorder. Construct, then call {@link start} to begin
 * capture; {@link stop} flushes the final partial chunk and releases
 * the microphone. Sequence numbers are issued internally — callers
 * cannot reorder them.
 */
export class WebRtcRecorder {
  private readonly options: Required<Pick<WebRtcRecorderOptions, "chunkIntervalMs" | "mimeType">> &
    WebRtcRecorderOptions;
  private mediaStream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private nextSeq = 0;
  private running = false;

  constructor(options: WebRtcRecorderOptions) {
    this.options = {
      chunkIntervalMs: DEFAULT_CHUNK_INTERVAL_MS,
      mimeType: DEFAULT_MIME_TYPE,
      ...options,
    };
  }

  /** True once {@link start} has resolved and chunks are flowing. */
  isRunning(): boolean {
    return this.running;
  }

  /** Begin capture. Resolves once the underlying MediaRecorder is started. */
  async start(): Promise<void> {
    if (this.running) {
      return;
    }
    const getUserMedia =
      this.options.getUserMedia ?? ((c) => navigator.mediaDevices.getUserMedia(c));
    const factory =
      this.options.recorderFactory ?? ((stream, opts) => new MediaRecorder(stream, opts));

    let stream: MediaStream;
    try {
      stream = await getUserMedia({ audio: true, video: false });
    } catch (err) {
      const wrapped = new WebRtcRecorderError(
        `microphone access denied: ${err instanceof Error ? err.message : String(err)}`,
      );
      this.options.onError?.(wrapped);
      throw wrapped;
    }
    this.mediaStream = stream;
    const rec = factory(stream, { mimeType: this.options.mimeType });
    rec.ondataavailable = (event: BlobEvent) => {
      if (!event.data || event.data.size === 0) {
        return;
      }
      const chunk: RecorderChunk = {
        seq: this.nextSeq++,
        blob: event.data,
        emittedAt: new Date().toISOString(),
      };
      // Caller errors must not break the recorder loop; they're
      // surfaced via onError so the UI can decide whether to abort.
      try {
        const ret = this.options.onChunk(chunk);
        if (ret && typeof (ret as Promise<void>).catch === "function") {
          (ret as Promise<void>).catch((err) =>
            this.options.onError?.(err instanceof Error ? err : new Error(String(err))),
          );
        }
      } catch (err) {
        this.options.onError?.(err instanceof Error ? err : new Error(String(err)));
      }
    };
    rec.onerror = (event: Event) => {
      const message =
        (event as unknown as { error?: { message?: string } })?.error?.message ??
        "MediaRecorder error";
      this.options.onError?.(new WebRtcRecorderError(message));
    };
    rec.onstop = () => {
      this.running = false;
      this.options.onStop?.();
    };
    this.recorder = rec;
    rec.start(this.options.chunkIntervalMs);
    this.running = true;
  }

  /** Stop capture; flushes the trailing partial chunk and releases the mic. */
  stop(): void {
    if (!this.running || !this.recorder) {
      return;
    }
    try {
      this.recorder.stop();
    } catch {
      // already-stopped recorders throw; safe to ignore.
    }
    if (this.mediaStream) {
      for (const track of this.mediaStream.getTracks()) {
        track.stop();
      }
    }
    this.mediaStream = null;
  }
}
