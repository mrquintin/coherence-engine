"use client";

import { useEffect, useState } from "react";
import { FormField, FormTextarea } from "./form_field";
import { FileUpload } from "./file_upload";
import { StepIndicator } from "./step_indicator";

const DRAFT_VERSION = 1;
const DRAFT_TTL_MS = 14 * 24 * 60 * 60 * 1000; // 14 days
const DRAFT_KEY_PREFIX = "fp.apply.draft.v1.";

export interface DraftFormState {
  version: number;
  saved_at: number;
  // Step 1: company
  full_name: string;
  email: string;
  company_name: string;
  country: string;
  // Step 2: product
  one_liner: string;
  // Step 3: market
  market_summary: string;
  // Step 4: ask
  requested_check_usd: string; // string while editing
  use_of_funds_summary: string;
  preferred_channel: "phone" | "web_voice" | "async_voice";
}

const EMPTY_DRAFT: DraftFormState = {
  version: DRAFT_VERSION,
  saved_at: 0,
  full_name: "",
  email: "",
  company_name: "",
  country: "US",
  one_liner: "",
  market_summary: "",
  requested_check_usd: "",
  use_of_funds_summary: "",
  preferred_channel: "web_voice",
};

const STEPS = [
  { id: "company", label: "Company" },
  { id: "product", label: "Product" },
  { id: "market", label: "Market" },
  { id: "ask", label: "Ask" },
  { id: "upload", label: "Upload" },
];

function loadDraft(draftId: string): DraftFormState {
  if (typeof window === "undefined") return EMPTY_DRAFT;
  try {
    const raw = window.localStorage.getItem(DRAFT_KEY_PREFIX + draftId);
    if (!raw) return EMPTY_DRAFT;
    const parsed = JSON.parse(raw) as Partial<DraftFormState>;
    if (
      typeof parsed.version !== "number" ||
      parsed.version !== DRAFT_VERSION ||
      typeof parsed.saved_at !== "number" ||
      Date.now() - parsed.saved_at > DRAFT_TTL_MS
    ) {
      return EMPTY_DRAFT;
    }
    return { ...EMPTY_DRAFT, ...parsed, version: DRAFT_VERSION };
  } catch {
    return EMPTY_DRAFT;
  }
}

function saveDraft(draftId: string, state: DraftFormState): void {
  if (typeof window === "undefined") return;
  try {
    const toPersist: DraftFormState = {
      ...state,
      version: DRAFT_VERSION,
      saved_at: Date.now(),
    };
    window.localStorage.setItem(DRAFT_KEY_PREFIX + draftId, JSON.stringify(toPersist));
  } catch {
    // Quota exceeded or storage disabled — silently skip; the user's session
    // still works, they just lose autosave.
  }
}

function clearDraft(draftId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(DRAFT_KEY_PREFIX + draftId);
  } catch {
    // ignore
  }
}

function newDraftId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `draft-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

interface SubmitOk {
  application_id: string;
}

interface Props {
  initialDraftId?: string;
  onSubmitted?: (applicationId: string) => void;
}

export function MultiStepApplicationForm({ initialDraftId, onSubmitted }: Props) {
  const [draftId] = useState<string>(() => initialDraftId ?? newDraftId());
  const [stepIndex, setStepIndex] = useState(0);
  const [draft, setDraft] = useState<DraftFormState>(EMPTY_DRAFT);
  const [hydrated, setHydrated] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [applicationId, setApplicationId] = useState<string | null>(null);
  const [savedHint, setSavedHint] = useState<string>("");

  useEffect(() => {
    setDraft(loadDraft(draftId));
    setHydrated(true);
  }, [draftId]);

  useEffect(() => {
    if (!hydrated) return;
    saveDraft(draftId, draft);
    setSavedHint(`Draft saved ${new Date().toLocaleTimeString()}`);
  }, [draft, draftId, hydrated]);

  function update<K extends keyof DraftFormState>(key: K, value: DraftFormState[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function validateStep(idx: number): Record<string, string> {
    const e: Record<string, string> = {};
    if (idx === 0) {
      if (!draft.full_name.trim()) e.full_name = "Required";
      if (!draft.email.includes("@")) e.email = "Valid email required";
      if (!draft.company_name.trim()) e.company_name = "Required";
      if (!draft.country.trim()) e.country = "Required";
    } else if (idx === 1) {
      if (draft.one_liner.trim().length < 10) e.one_liner = "At least 10 characters";
    } else if (idx === 2) {
      if (draft.market_summary.trim().length < 10) e.market_summary = "At least 10 characters";
    } else if (idx === 3) {
      const n = Number(draft.requested_check_usd);
      if (!Number.isFinite(n) || n <= 0) e.requested_check_usd = "Enter a positive number";
      if (draft.use_of_funds_summary.trim().length < 10)
        e.use_of_funds_summary = "At least 10 characters";
    }
    return e;
  }

  function next() {
    const e = validateStep(stepIndex);
    setErrors(e);
    if (Object.keys(e).length === 0) {
      setStepIndex((i) => Math.min(i + 1, STEPS.length - 1));
    }
  }

  function prev() {
    setStepIndex((i) => Math.max(i - 1, 0));
  }

  async function submit() {
    setSubmitting(true);
    setSubmitError(null);
    const payload = {
      founder: {
        full_name: draft.full_name.trim(),
        email: draft.email.trim(),
        company_name: draft.company_name.trim(),
        country: draft.country.trim(),
      },
      startup: {
        one_liner: draft.one_liner.trim(),
        requested_check_usd: Number(draft.requested_check_usd),
        use_of_funds_summary:
          draft.use_of_funds_summary.trim() || `Market: ${draft.market_summary.slice(0, 200)}`,
        preferred_channel: draft.preferred_channel,
      },
      consent: {
        ai_assessment: true,
        recording: true,
        data_processing: true,
      },
    };
    try {
      const res = await fetch("/api/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "create_application", payload }),
      });
      const body = (await res.json()) as Partial<SubmitOk> & { error?: string };
      if (!res.ok || !body.application_id) {
        setSubmitError(body.error ?? `Submission failed (${res.status})`);
      } else {
        setApplicationId(body.application_id);
        clearDraft(draftId);
        setStepIndex(STEPS.length - 1);
        onSubmitted?.(body.application_id);
      }
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Network error");
    } finally {
      setSubmitting(false);
    }
  }

  const isLastEditingStep = stepIndex === STEPS.length - 2; // "Ask" — last data step
  const isUploadStep = stepIndex === STEPS.length - 1;

  return (
    <div className="space-y-6">
      <StepIndicator steps={STEPS} currentIndex={stepIndex} />

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (isLastEditingStep) {
            const errs = validateStep(stepIndex);
            setErrors(errs);
            if (Object.keys(errs).length === 0) {
              void submit();
            }
          } else if (!isUploadStep) {
            next();
          }
        }}
        className="space-y-5 rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
        aria-label="Application form"
        noValidate
      >
        {stepIndex === 0 ? (
          <fieldset className="space-y-4" aria-labelledby="step-company-legend">
            <legend id="step-company-legend" className="text-lg font-semibold">
              About you and your company
            </legend>
            <FormField
              label="Founder name"
              required
              value={draft.full_name}
              error={errors.full_name}
              onChange={(e) => update("full_name", e.target.value)}
              data-testid="full_name"
            />
            <FormField
              label="Founder email"
              type="email"
              required
              value={draft.email}
              error={errors.email}
              onChange={(e) => update("email", e.target.value)}
              data-testid="email"
            />
            <FormField
              label="Company name"
              required
              value={draft.company_name}
              error={errors.company_name}
              onChange={(e) => update("company_name", e.target.value)}
              data-testid="company_name"
            />
            <FormField
              label="Country"
              required
              hint="Two-letter country code (e.g. US, GB)"
              value={draft.country}
              error={errors.country}
              onChange={(e) => update("country", e.target.value)}
              data-testid="country"
            />
          </fieldset>
        ) : null}

        {stepIndex === 1 ? (
          <fieldset className="space-y-4" aria-labelledby="step-product-legend">
            <legend id="step-product-legend" className="text-lg font-semibold">
              Product
            </legend>
            <FormTextarea
              label="One-liner"
              required
              hint="What does your company do, in one sentence?"
              value={draft.one_liner}
              error={errors.one_liner}
              onChange={(e) => update("one_liner", e.target.value)}
              data-testid="one_liner"
            />
          </fieldset>
        ) : null}

        {stepIndex === 2 ? (
          <fieldset className="space-y-4" aria-labelledby="step-market-legend">
            <legend id="step-market-legend" className="text-lg font-semibold">
              Market
            </legend>
            <FormTextarea
              label="Who are you selling to, and how big is the market?"
              required
              value={draft.market_summary}
              error={errors.market_summary}
              onChange={(e) => update("market_summary", e.target.value)}
              data-testid="market_summary"
            />
          </fieldset>
        ) : null}

        {stepIndex === 3 ? (
          <fieldset className="space-y-4" aria-labelledby="step-ask-legend">
            <legend id="step-ask-legend" className="text-lg font-semibold">
              Ask
            </legend>
            <FormField
              label="Requested check (USD)"
              type="number"
              required
              min={1}
              value={draft.requested_check_usd}
              error={errors.requested_check_usd}
              onChange={(e) => update("requested_check_usd", e.target.value)}
              data-testid="requested_check_usd"
            />
            <FormTextarea
              label="Use of funds"
              required
              hint="Hires, runway, milestones."
              value={draft.use_of_funds_summary}
              error={errors.use_of_funds_summary}
              onChange={(e) => update("use_of_funds_summary", e.target.value)}
              data-testid="use_of_funds_summary"
            />
            <div className="space-y-1">
              <label
                htmlFor="preferred_channel"
                className="block text-sm font-medium text-slate-700"
              >
                Preferred interview channel
              </label>
              <select
                id="preferred_channel"
                value={draft.preferred_channel}
                onChange={(e) =>
                  update("preferred_channel", e.target.value as DraftFormState["preferred_channel"])
                }
                className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 shadow-sm focus:border-slate-500 focus:outline-none focus:ring-2 focus:ring-slate-400"
              >
                <option value="web_voice">Web voice</option>
                <option value="phone">Phone</option>
                <option value="async_voice">Async voice</option>
              </select>
            </div>
          </fieldset>
        ) : null}

        {stepIndex === 4 ? (
          <fieldset className="space-y-4" aria-labelledby="step-upload-legend">
            <legend id="step-upload-legend" className="text-lg font-semibold">
              Upload supporting materials
            </legend>
            {applicationId ? (
              <>
                <p className="text-sm text-slate-600">
                  Application created. You can attach a deck and any supporting documents now, or
                  skip this step and add them later from your application page.
                </p>
                <FileUpload
                  applicationId={applicationId}
                  kind="deck"
                  label="Pitch deck"
                  hint="PDF or PowerPoint, up to 25 MB."
                  acceptTypes=".pdf,.pptx,.ppt,application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/vnd.ms-powerpoint"
                />
                <FileUpload
                  applicationId={applicationId}
                  kind="supporting"
                  label="Supporting document (optional)"
                  hint="Up to 25 MB. PDF, PNG, JPEG."
                  acceptTypes=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"
                />
                <a
                  href={`/applications/${applicationId}`}
                  className="inline-block rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
                >
                  View application status
                </a>
              </>
            ) : (
              <p className="text-sm text-slate-600">
                Submit the form on the previous step to enable file uploads.
              </p>
            )}
          </fieldset>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 pt-4">
          <span className="text-xs text-slate-500" aria-live="polite">
            {savedHint}
          </span>
          <div className="flex gap-2">
            {stepIndex > 0 ? (
              <button
                type="button"
                onClick={prev}
                disabled={submitting}
                className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-60"
              >
                Back
              </button>
            ) : null}
            {!isLastEditingStep && !isUploadStep ? (
              <button
                type="button"
                onClick={next}
                disabled={submitting}
                className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-60"
                data-testid="next-step"
              >
                Next
              </button>
            ) : null}
            {isLastEditingStep ? (
              <button
                type="submit"
                disabled={submitting}
                className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-60"
                data-testid="submit-application"
              >
                {submitting ? "Submitting…" : "Submit application"}
              </button>
            ) : null}
          </div>
        </div>

        {submitError ? (
          <p
            className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900"
            role="alert"
          >
            {submitError}
          </p>
        ) : null}
      </form>
    </div>
  );
}
