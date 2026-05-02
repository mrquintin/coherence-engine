"use client";

import { useEffect, useState } from "react";
import { isTerminalStatus, useApplicationStatus } from "@/hooks/use_application_status";
import { StatusTimeline, buildTimelineFromStatus } from "./status_timeline";

interface Props {
  applicationId: string;
}

interface ArtifactPayload {
  artifact_id?: string;
  payload?: Record<string, unknown>;
  error?: string;
}

export function ApplicationStatusView({ applicationId }: Props) {
  const { status, error, isPolling, lastPolledAt } = useApplicationStatus(applicationId);
  const [artifact, setArtifact] = useState<ArtifactPayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (status && isTerminalStatus(status.status)) {
      void (async () => {
        try {
          const res = await fetch(
            `/api/auth?action=artifact_url&application_id=${encodeURIComponent(applicationId)}`,
            { cache: "no-store" },
          );
          if (!res.ok) return;
          const body = (await res.json()) as ArtifactPayload;
          if (!cancelled) setArtifact(body);
        } catch {
          // best-effort
        }
      })();
    }
    return () => {
      cancelled = true;
    };
  }, [applicationId, status]);

  const timeline = buildTimelineFromStatus(status?.status ?? null, artifact?.payload ?? null);

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="text-lg font-semibold">Current status</h2>
        <p className="mt-1 text-2xl font-semibold capitalize" data-testid="application-status">
          {status?.status?.replace(/_/g, " ") ?? "loading…"}
        </p>
        <p className="mt-1 text-xs text-slate-500" aria-live="polite">
          {isPolling ? "Polling every 5s for updates…" : "Polling stopped (terminal state)."}
          {lastPolledAt ? ` Last update ${new Date(lastPolledAt).toLocaleTimeString()}.` : null}
        </p>
        {error ? (
          <p
            className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
            role="alert"
          >
            Last poll failed: {error}. Backing off and retrying.
          </p>
        ) : null}
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Timeline</h2>
        <StatusTimeline stages={timeline} />
      </section>

      {artifact?.payload ? (
        <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-lg font-semibold">Decision artifact</h2>
          <p className="mt-1 text-sm text-slate-600">
            Artifact ID: <code className="font-mono text-xs">{artifact.artifact_id}</code>
          </p>
          <details className="mt-2">
            <summary className="cursor-pointer text-sm underline">View artifact payload</summary>
            <pre className="mt-2 max-h-96 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
              {JSON.stringify(artifact.payload, null, 2)}
            </pre>
          </details>
        </section>
      ) : null}
    </div>
  );
}
