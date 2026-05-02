"use client";

interface Step {
  id: string;
  label: string;
}

interface Props {
  steps: Step[];
  currentIndex: number;
}

export function StepIndicator({ steps, currentIndex }: Props) {
  return (
    <nav aria-label="Application progress" className="mb-6">
      <ol className="flex flex-wrap gap-2">
        {steps.map((step, idx) => {
          const isCurrent = idx === currentIndex;
          const isComplete = idx < currentIndex;
          const className = isCurrent
            ? "border-slate-900 bg-slate-900 text-white"
            : isComplete
              ? "border-emerald-300 bg-emerald-50 text-emerald-900"
              : "border-slate-300 bg-white text-slate-600";
          return (
            <li
              key={step.id}
              className={`flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium ${className}`}
              aria-current={isCurrent ? "step" : undefined}
            >
              <span aria-hidden="true" className="font-mono">
                {idx + 1}
              </span>
              <span>{step.label}</span>
              {isComplete ? (
                <span className="sr-only"> (completed)</span>
              ) : isCurrent ? (
                <span className="sr-only"> (current step)</span>
              ) : null}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
