/**
 * Direct-PUT upload helpers backed by signed URLs from the backend.
 *
 * Two-phase flow:
 *   1. POST /api/auth { action: "upload_initiate", application_id, ... }
 *      → backend mints a signed_url with `expires_at` and returns upload metadata.
 *   2. PUT bytes directly to the signed URL (no proxy through Next.js).
 *   3. POST /api/auth { action: "upload_complete", application_id, upload_id }
 *      → backend verifies the object in storage and persists the URI.
 *
 * The browser never sees the storage credentials; the server-issued signed_url
 * is the only authority needed for the PUT.
 */

export interface InitiateUploadInput {
  application_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  kind: "deck" | "supporting";
}

export interface InitiateUploadResult {
  upload_id: string;
  upload_url: string;
  headers: Record<string, string>;
  expires_at: string;
  key: string;
  uri: string;
  max_bytes: number;
}

export interface CompleteUploadResult {
  upload_id: string;
  uri: string;
  size_bytes: number;
  status: "completed";
}

export class UploadError extends Error {
  readonly phase: "initiate" | "put" | "complete";
  constructor(phase: "initiate" | "put" | "complete", message: string) {
    super(message);
    this.phase = phase;
    this.name = "UploadError";
  }
}

async function postJson<T>(body: unknown): Promise<T> {
  const res = await fetch("/api/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let parsed: unknown = null;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new Error(`non-JSON response (status ${res.status})`);
    }
  }
  if (!res.ok) {
    const msg = (parsed as { error?: string } | null)?.error ?? `request failed (${res.status})`;
    throw new Error(msg);
  }
  return parsed as T;
}

export async function initiateUpload(input: InitiateUploadInput): Promise<InitiateUploadResult> {
  try {
    return await postJson<InitiateUploadResult>({
      action: "upload_initiate",
      ...input,
    });
  } catch (err) {
    throw new UploadError("initiate", err instanceof Error ? err.message : String(err));
  }
}

export async function putToSignedUrl(
  signed_url: string,
  headers: Record<string, string>,
  file: File,
  onProgress?: (loadedFraction: number) => void,
): Promise<void> {
  // Use XMLHttpRequest so we can surface upload progress; fetch() doesn't
  // expose Upload progress in browsers as of 2026.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", signed_url, true);
    for (const [k, v] of Object.entries(headers)) {
      try {
        xhr.setRequestHeader(k, v);
      } catch {
        // Some restricted headers (Content-Length) cannot be set; ignore.
      }
    }
    xhr.upload.onprogress = (ev) => {
      if (onProgress && ev.lengthComputable && ev.total > 0) {
        onProgress(ev.loaded / ev.total);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new UploadError("put", `upload failed (${xhr.status} ${xhr.statusText})`));
      }
    };
    xhr.onerror = () => reject(new UploadError("put", "network error during upload"));
    xhr.onabort = () => reject(new UploadError("put", "upload aborted"));
    xhr.send(file);
  });
}

export async function completeUpload(
  application_id: string,
  upload_id: string,
): Promise<CompleteUploadResult> {
  try {
    return await postJson<CompleteUploadResult>({
      action: "upload_complete",
      application_id,
      upload_id,
    });
  } catch (err) {
    throw new UploadError("complete", err instanceof Error ? err.message : String(err));
  }
}

export interface UploadFileOptions {
  application_id: string;
  file: File;
  kind: "deck" | "supporting";
  onProgress?: (loadedFraction: number) => void;
}

/** End-to-end helper: initiate → PUT → complete. Returns the canonical URI. */
export async function uploadFile(opts: UploadFileOptions): Promise<CompleteUploadResult> {
  const initiated = await initiateUpload({
    application_id: opts.application_id,
    filename: opts.file.name,
    content_type: opts.file.type || "application/octet-stream",
    size_bytes: opts.file.size,
    kind: opts.kind,
  });
  await putToSignedUrl(initiated.upload_url, initiated.headers, opts.file, opts.onProgress);
  return completeUpload(opts.application_id, initiated.upload_id);
}
