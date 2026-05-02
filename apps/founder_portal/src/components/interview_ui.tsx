"use client";

/**
 * Interview UI for the in-page WebRTC founder interview (prompt 39).
 *
 * Owns lifecycle for the {@link WebRtcRecorder}: start capture on
 * "Start interview", funnel each emitted chunk through the chunked-
 * upload pipeline, and call ``finalize`` when the founder ends the
 * session. State is intentionally scoped to this component — the
 * interview flow is one page; persistent state is the responsibility
 * of the backend (chunk rows + interview_session).
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  finalizeSession,
  uploadChunk,
  type CompleteChunkResult,
  type FinalizeSessionResult,
} from "@/lib/chunked_upload";
import {
  WebRtcRecorder,
  type RecorderChunk,
  type WebRtcRecorderOptions,
} from "@/lib/webrtc_recorder";

type Phase = "idle" | "recording" | "finalizing" | "completed" | "error";

export interface InterviewUiProps {
  sessionId: string;
  /** Test seam — defaults to the platform recorder. */
  recorderOverride?: Partial<WebRtcRecorderOptions>;
}

export function InterviewUi({ sessionId, recorderOverride }: InterviewUiProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [chunkCount, setChunkCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [finalResult, setFinalResult] = useState<FinalizeSessionResult | null>(null);
  const recorderRef = useRef<WebRtcRecorder | null>(null);
  // Serialize uploads behind a single promise chain so the
  // recorder's onChunk can fire faster than the network round-trip
  // without losing seq ordering.
  const uploadQueueRef = useRef<Promise<CompleteChunkResult | void>>(Promise.resolve());

  const handleChunk = useCallback(
    (chunk: RecorderChunk) => {
      uploadQueueRef.current = uploadQueueRef.current
        .then(() => uploadChunk(sessionId, chunk))
        .then((result) => {
          setChunkCount((n) => n + 1);
          return result;
        })
        .catch((err) => {
          setError(err instanceof Error ? err.message : String(err));
          setPhase("error");
          // Re-throw to break the chain: subsequent chunks see the
          // rejected queue and will themselves reject. The user can
          // restart from the UI.
          throw err;
        });
      return uploadQueueRef.current as Promise<void>;
    },
    [sessionId],
  );

  const startRecording = useCallback(async () => {
    setError(null);
    setFinalResult(null);
    setChunkCount(0);
    uploadQueueRef.current = Promise.resolve();
    const rec = new WebRtcRecorder({
      onChunk: handleChunk,
      onError: (err) => {
        setError(err.message);
        setPhase("error");
      },
      ...recorderOverride,
    });
    recorderRef.current = rec;
    try {
      await rec.start();
      setPhase("recording");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    }
  }, [handleChunk, recorderOverride]);

  const stopAndFinalize = useCallback(async () => {
    const rec = recorderRef.current;
    if (rec) {
      rec.stop();
    }
    setPhase("finalizing");
    try {
      // Wait for in-flight uploads to drain before asking the server
      // to stitch — otherwise a late-arriving chunk would race the
      // finalize call.
      await uploadQueueRef.current.catch(() => undefined);
      const result = await finalizeSession(sessionId);
      setFinalResult(result);
      setPhase("completed");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    }
  }, [sessionId]);

  useEffect(() => {
    return () => {
      recorderRef.current?.stop();
    };
  }, []);

  return (
    <div className="space-y-6" data-testid="interview-ui">
      <div className="space-y-2">
        <p className="text-sm text-slate-500">
          Session ID: <code data-testid="session-id">{sessionId}</code>
        </p>
        <p className="text-sm text-slate-500">
          Chunks uploaded: <span data-testid="chunk-count">{chunkCount}</span>
        </p>
        <p className="text-sm text-slate-500">
          Phase: <span data-testid="phase">{phase}</span>
        </p>
      </div>
      <div className="flex gap-3">
        <button
          type="button"
          data-testid="start-interview"
          disabled={phase === "recording" || phase === "finalizing"}
          onClick={startRecording}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          Start interview
        </button>
        <button
          type="button"
          data-testid="stop-interview"
          disabled={phase !== "recording"}
          onClick={stopAndFinalize}
          className="rounded border border-slate-300 px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          End and submit
        </button>
      </div>
      {error ? (
        <p data-testid="interview-error" className="text-sm text-rose-600">
          {error}
        </p>
      ) : null}
      {finalResult ? (
        <pre data-testid="interview-result" className="rounded bg-slate-50 p-3 text-xs">
          {JSON.stringify(finalResult, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}
