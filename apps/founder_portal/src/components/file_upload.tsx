"use client";

import { useId, useState } from "react";
import { uploadFile, type CompleteUploadResult } from "@/lib/upload";

interface Props {
  applicationId: string;
  kind: "deck" | "supporting";
  label: string;
  hint?: string;
  acceptTypes?: string;
  onUploaded?: (result: CompleteUploadResult) => void;
}

interface UploadState {
  status: "idle" | "uploading" | "done" | "error";
  progress: number;
  message?: string;
  uri?: string;
}

export function FileUpload({ applicationId, kind, label, hint, acceptTypes, onUploaded }: Props) {
  const id = useId();
  const [state, setState] = useState<UploadState>({ status: "idle", progress: 0 });

  async function handleFile(file: File) {
    setState({ status: "uploading", progress: 0 });
    try {
      const result = await uploadFile({
        application_id: applicationId,
        file,
        kind,
        onProgress: (frac) => setState((s) => ({ ...s, progress: Math.round(frac * 100) })),
      });
      setState({
        status: "done",
        progress: 100,
        message: `Uploaded (${result.size_bytes.toLocaleString()} bytes).`,
        uri: result.uri,
      });
      onUploaded?.(result);
    } catch (err) {
      setState({
        status: "error",
        progress: 0,
        message: err instanceof Error ? err.message : "upload failed",
      });
    }
  }

  return (
    <div className="space-y-2">
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        id={id}
        type="file"
        accept={acceptTypes}
        aria-describedby={hint ? `${id}-hint` : undefined}
        disabled={state.status === "uploading"}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) {
            void handleFile(file);
          }
        }}
        className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm file:mr-3 file:rounded file:border-0 file:bg-slate-900 file:px-3 file:py-1.5 file:text-white focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-60"
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
      {state.status === "uploading" ? (
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={state.progress}
          aria-label={`Uploading ${kind}`}
          className="h-2 w-full overflow-hidden rounded bg-slate-200"
        >
          <div
            className="h-full bg-slate-900 transition-all"
            style={{ width: `${state.progress}%` }}
          />
        </div>
      ) : null}
      {state.status === "done" && state.message ? (
        <p
          className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-900"
          role="status"
        >
          {state.message}
        </p>
      ) : null}
      {state.status === "error" && state.message ? (
        <p
          className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-900"
          role="alert"
        >
          {state.message}
        </p>
      ) : null}
    </div>
  );
}
