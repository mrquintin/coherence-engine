"use client";

interface TimelineStage {
  id: string;
  label: string;
  state: "pending" | "active" | "complete" | "failed";
  detail?: string;
  timestamp?: string;
}

interface Props {
  stages: TimelineStage[];
}

const STATE_TONE: Record<TimelineStage["state"], string> = {
  pending: "border-slate-200 bg-white text-slate-500",
  active: "border-amber-300 bg-amber-50 text-amber-900",
  complete: "border-emerald-300 bg-emerald-50 text-emerald-900",
  failed: "border-rose-300 bg-rose-50 text-rose-900",
};

const STATE_LABEL: Record<TimelineStage["state"], string> = {
  pending: "Pending",
  active: "In progress",
  complete: "Complete",
  failed: "Failed",
};

export function StatusTimeline({ stages }: Props) {
  return (
    <ol aria-label="Application processing stages" className="space-y-3">
      {stages.map((stage) => (
        <li key={stage.id} className={`rounded-lg border p-4 shadow-sm ${STATE_TONE[stage.state]}`}>
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="text-sm font-semibold">{stage.label}</h3>
            <span className="text-xs uppercase tracking-wide" aria-hidden="true">
              {STATE_LABEL[stage.state]}
            </span>
            <span className="sr-only">Stage status: {STATE_LABEL[stage.state]}</span>
          </div>
          {stage.detail ? <p className="mt-1 text-xs">{stage.detail}</p> : null}
          {stage.timestamp ? (
            <p className="mt-1 text-xs text-slate-500">
              <time dateTime={stage.timestamp}>{stage.timestamp}</time>
            </p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

const STATUS_ORDER = [
  "intake_created",
  "interview_in_progress",
  "scoring_queued",
  "scoring_in_progress",
  "decision_pass",
  "decision_fail",
  "decision_manual_review",
];

const STAGES = [
  { id: "intake", label: "Intake", match: ["intake_created"] },
  {
    id: "interview",
    label: "Interview",
    match: ["interview_in_progress"],
  },
  {
    id: "scoring",
    label: "Scoring",
    match: ["scoring_queued", "scoring_in_progress", "scoring_retry_pending"],
  },
  {
    id: "decision",
    label: "Decision",
    match: ["decision_pass", "decision_fail", "decision_manual_review"],
  },
];

export function buildTimelineFromStatus(
  status: string | null,
  artifactPayload?: Record<string, unknown> | null,
): TimelineStage[] {
  const idx = status ? STATUS_ORDER.indexOf(status) : -1;
  return STAGES.map((stage, i) => {
    const matchIdxs = stage.match.map((s) => STATUS_ORDER.indexOf(s));
    const isMatched = matchIdxs.includes(idx);
    const isPast = idx > Math.max(...matchIdxs);
    let state: TimelineStage["state"];
    if (idx < 0) {
      state = i === 0 ? "active" : "pending";
    } else if (isMatched) {
      state = stage.id === "decision" ? "complete" : "active";
    } else if (isPast) {
      state = "complete";
    } else {
      state = "pending";
    }
    let detail: string | undefined;
    if (stage.id === "decision" && artifactPayload) {
      const artifactDecision = (artifactPayload as { decision?: { decision?: string } }).decision
        ?.decision;
      if (artifactDecision) {
        detail = `Verdict: ${artifactDecision}`;
      }
    }
    return { id: stage.id, label: stage.label, state, detail };
  });
}
