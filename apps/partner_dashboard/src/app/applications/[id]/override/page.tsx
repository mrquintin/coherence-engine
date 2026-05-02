import Link from "next/link";
import { redirect } from "next/navigation";
import {
  createPartnerApiClient,
  PartnerApiError,
  type OverrideWriteRequest,
} from "@/lib/api_client";
import { getServerSession, isPartnerOrAdmin } from "@/lib/supabase";
import { MIN_REASON_TEXT_LENGTH, validateOverrideForm } from "./validate";

export const dynamic = "force-dynamic";

interface OverridePageProps {
  params: { id: string };
  searchParams?: Record<string, string | string[] | undefined>;
}

const REASON_CODES: { value: OverrideWriteRequest["reason_code"]; label: string }[] = [
  { value: "factual_error", label: "Factual error" },
  { value: "policy_misalignment", label: "Policy misalignment" },
  { value: "regulatory_constraint", label: "Regulatory constraint" },
  { value: "manual_diligence", label: "Manual diligence" },
];

const VERDICTS: OverrideWriteRequest["override_verdict"][] = ["pass", "reject", "manual_review"];

export default async function OverridePage({ params, searchParams }: OverridePageProps) {
  const session = await getServerSession();
  if (!isPartnerOrAdmin(session.role)) {
    return (
      <section className="rounded border border-amber-300 bg-amber-50 p-6">
        <h1 className="text-2xl font-semibold">Access required</h1>
        <p className="mt-2 text-sm text-slate-700">
          Only <code>partner</code> or <code>admin</code> roles may submit overrides.
        </p>
      </section>
    );
  }

  const errorParam = pickFirst(searchParams?.error);

  return (
    <section className="space-y-6">
      <Link href={`/applications/${params.id}`} className="text-sm text-slate-600 hover:underline">
        ← back to application
      </Link>
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Override decision</h1>
        <p className="text-sm text-slate-600">
          Application <span className="font-mono">{params.id}</span>. The original automated
          decision will be preserved as the audit trail — this writes a separate ledger row.
        </p>
      </header>

      {errorParam ? (
        <p className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {errorParam}
        </p>
      ) : null}

      <form
        action={submitOverride.bind(null, params.id)}
        className="space-y-4 rounded border border-slate-200 bg-white p-6"
        data-testid="override-form"
      >
        <label className="flex flex-col text-sm">
          <span className="text-slate-700">New verdict</span>
          <select
            name="override_verdict"
            required
            defaultValue="manual_review"
            className="mt-1 rounded border border-slate-300 px-2 py-1.5"
          >
            {VERDICTS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-sm">
          <span className="text-slate-700">Reason code</span>
          <select
            name="reason_code"
            required
            className="mt-1 rounded border border-slate-300 px-2 py-1.5"
          >
            {REASON_CODES.map((rc) => (
              <option key={rc.value} value={rc.value}>
                {rc.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-sm">
          <span className="text-slate-700">Reason text (≥ {MIN_REASON_TEXT_LENGTH} chars)</span>
          <textarea
            name="reason_text"
            required
            minLength={MIN_REASON_TEXT_LENGTH}
            rows={5}
            className="mt-1 rounded border border-slate-300 px-2 py-1.5 font-mono text-sm"
          />
        </label>

        <label className="flex flex-col text-sm">
          <span className="text-slate-700">
            Justification memo URI (required for pass → reject)
          </span>
          <input
            type="text"
            name="justification_uri"
            placeholder="s3://memos/<file>.pdf"
            className="mt-1 rounded border border-slate-300 px-2 py-1.5 font-mono text-sm"
          />
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" name="unrevise" value="true" />
          <span>Force unrevise (supersedes any prior active override)</span>
        </label>

        <div className="flex justify-end gap-2">
          <Link
            href={`/applications/${params.id}`}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm"
          >
            Cancel
          </Link>
          <button
            type="submit"
            className="rounded bg-slate-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          >
            Submit override
          </button>
        </div>
      </form>
    </section>
  );
}

function pickFirst(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return value ?? "";
}

async function submitOverride(applicationId: string, formData: FormData) {
  "use server";
  const session = await getServerSession();
  if (!isPartnerOrAdmin(session.role)) {
    redirect(
      `/applications/${applicationId}/override?error=` +
        encodeURIComponent("Forbidden: partner role required"),
    );
  }

  const raw = {
    override_verdict: String(formData.get("override_verdict") ?? ""),
    reason_code: String(formData.get("reason_code") ?? ""),
    reason_text: String(formData.get("reason_text") ?? ""),
    justification_uri: String(formData.get("justification_uri") ?? ""),
    unrevise: formData.get("unrevise") === "true",
  };

  const validation = validateOverrideForm(raw);
  if (!validation.ok) {
    redirect(
      `/applications/${applicationId}/override?error=` + encodeURIComponent(validation.error),
    );
  }

  const client = createPartnerApiClient(session.accessToken);
  try {
    await client.postOverride(applicationId, validation.value);
  } catch (err) {
    const msg =
      err instanceof PartnerApiError ? `${err.code}: ${err.message}` : (err as Error).message;
    redirect(`/applications/${applicationId}/override?error=` + encodeURIComponent(msg));
  }
  redirect(`/applications/${applicationId}`);
}
