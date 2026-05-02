"use client";

import { MultiStepApplicationForm } from "@/components/multi_step_form";

export default function ApplyPage() {
  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm font-medium uppercase tracking-wide text-slate-500">
          New application
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">Apply to the Coherence Fund</h1>
        <p className="text-slate-600">
          A short multi-step form. Your draft is autosaved locally — you can close the tab and
          resume from this device.
        </p>
      </header>
      <MultiStepApplicationForm />
    </div>
  );
}
