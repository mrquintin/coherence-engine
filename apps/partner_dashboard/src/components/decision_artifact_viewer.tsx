/**
 * Tree-view of a decision_artifact JSON document with collapsible
 * sections, per_layer score bars, and the policy reason-code list.
 *
 * Verification markers: ``decision_artifact|per_layer``.
 */

import type { DecisionArtifact } from "@/lib/api_client";

export interface DecisionArtifactViewerProps {
  artifact: DecisionArtifact | null;
  perLayer?: Record<string, number> | null;
}

function ScoreBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
  return (
    <div className="h-2 w-full overflow-hidden rounded bg-slate-200" aria-hidden>
      <div
        className="h-full rounded bg-slate-700"
        style={{ width: `${(pct * 100).toFixed(1)}%` }}
      />
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-3 gap-2 border-b border-slate-100 py-1.5 text-sm">
      <dt className="col-span-1 text-slate-600">{label}</dt>
      <dd className="col-span-2 font-mono text-xs text-slate-900">{value}</dd>
    </div>
  );
}

export function DecisionArtifactViewer({ artifact, perLayer }: DecisionArtifactViewerProps) {
  if (!artifact) {
    return (
      <section className="rounded border border-dashed border-slate-300 bg-white p-4 text-sm text-slate-500">
        No decision artifact available for this application yet.
      </section>
    );
  }

  const failedGates = Array.isArray(artifact.failed_gates)
    ? (artifact.failed_gates as unknown[])
    : [];

  return (
    <section
      className="space-y-4 rounded border border-slate-200 bg-white p-4"
      data-testid="decision-artifact-viewer"
    >
      <details open className="space-y-2">
        <summary className="cursor-pointer text-sm font-semibold text-slate-800">
          Decision summary
        </summary>
        <dl className="mt-2">
          <Row label="Verdict" value={artifact.decision} />
          <Row label="Policy version" value={artifact.policy_version} />
          <Row label="Decision policy version" value={artifact.decision_policy_version ?? "—"} />
          <Row label="Parameter set" value={artifact.parameter_set_id} />
          <Row label="Threshold required" value={artifact.threshold_required.toFixed(3)} />
          <Row label="Coherence observed" value={artifact.coherence_observed.toFixed(3)} />
          <Row label="Margin" value={artifact.margin.toFixed(3)} />
        </dl>
      </details>

      <details open className="space-y-2">
        <summary className="cursor-pointer text-sm font-semibold text-slate-800">
          Coherence vs threshold
        </summary>
        <div className="mt-2 space-y-2">
          <div className="flex items-center gap-3 text-xs text-slate-600">
            <span className="w-32">Threshold</span>
            <ScoreBar value={artifact.threshold_required} />
            <span className="w-16 text-right font-mono">
              {artifact.threshold_required.toFixed(3)}
            </span>
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-600">
            <span className="w-32">Observed</span>
            <ScoreBar value={artifact.coherence_observed} />
            <span className="w-16 text-right font-mono">
              {artifact.coherence_observed.toFixed(3)}
            </span>
          </div>
        </div>
      </details>

      {perLayer && Object.keys(perLayer).length > 0 ? (
        <details open className="space-y-2">
          <summary className="cursor-pointer text-sm font-semibold text-slate-800">
            Per-layer scores (per_layer)
          </summary>
          <div className="mt-2 space-y-2">
            {Object.entries(perLayer).map(([layer, score]) => (
              <div key={layer} className="flex items-center gap-3 text-xs text-slate-600">
                <span className="w-32">{layer}</span>
                <ScoreBar value={Number(score)} />
                <span className="w-16 text-right font-mono">{Number(score).toFixed(3)}</span>
              </div>
            ))}
          </div>
        </details>
      ) : null}

      <details className="space-y-2">
        <summary className="cursor-pointer text-sm font-semibold text-slate-800">
          Failed gates / reason codes ({failedGates.length})
        </summary>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
          {failedGates.length === 0 ? (
            <li className="list-none text-slate-500">No failed gates.</li>
          ) : (
            failedGates.map((g, i) => (
              <li key={i} className="font-mono text-xs">
                {typeof g === "string" ? g : JSON.stringify(g)}
              </li>
            ))
          )}
        </ul>
      </details>
    </section>
  );
}
