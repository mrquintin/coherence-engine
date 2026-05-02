import Link from "next/link";
import { createPartnerApiClient, PartnerApiError } from "@/lib/api_client";
import { DecisionArtifactViewer } from "@/components/decision_artifact_viewer";
import { getServerSession, isPartnerOrAdmin } from "@/lib/supabase";

export const dynamic = "force-dynamic";

interface ApplicationPageProps {
  params: { id: string };
}

export default async function ApplicationDetailPage({ params }: ApplicationPageProps) {
  const session = await getServerSession();
  if (!isPartnerOrAdmin(session.role)) {
    return (
      <section className="rounded border border-amber-300 bg-amber-50 p-6">
        <h1 className="text-2xl font-semibold">Access required</h1>
        <p className="mt-2 text-sm text-slate-700">
          You need a <code>partner</code> or <code>admin</code> role to view application details.
        </p>
      </section>
    );
  }

  const client = createPartnerApiClient(session.accessToken);
  let detail: Awaited<ReturnType<typeof client.getApplication>>["data"] | null = null;
  let errorMessage: string | null = null;
  try {
    const response = await client.getApplication(params.id);
    detail = response.data;
  } catch (err) {
    errorMessage =
      err instanceof PartnerApiError
        ? `${err.status} ${err.code}: ${err.message}`
        : (err as Error).message;
  }

  if (!detail) {
    return (
      <section className="space-y-4">
        <Link href="/pipeline" className="text-sm text-slate-600 hover:underline">
          ← back to pipeline
        </Link>
        <p className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {errorMessage ?? "Application not found."}
        </p>
      </section>
    );
  }

  const override = detail.override;

  return (
    <section className="space-y-6">
      <Link href="/pipeline" className="text-sm text-slate-600 hover:underline">
        ← back to pipeline
      </Link>
      <header className="flex items-baseline justify-between">
        <div>
          <h1 className="font-mono text-xl">{detail.application_id}</h1>
          <p className="text-sm text-slate-600">
            Domain: {detail.domain_primary || "—"} · Mode: {detail.scoring_mode} · Status:{" "}
            {detail.status}
          </p>
        </div>
        <Link
          href={`/applications/${detail.application_id}/override`}
          className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          data-testid="override-cta"
        >
          {override ? "Revise override" : "Override decision"}
        </Link>
      </header>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="space-y-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600">
            Decision artifact
          </h2>
          <DecisionArtifactViewer artifact={detail.decision_artifact} />
        </div>

        <div className="space-y-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600">
            Override status
          </h2>
          {override ? (
            <article className="rounded border border-amber-300 bg-amber-50 p-4 text-sm">
              <p className="font-semibold">Active override: {override.override_verdict}</p>
              <dl className="mt-2 space-y-1 text-xs">
                <div>
                  <dt className="inline text-slate-600">Reason code: </dt>
                  <dd className="inline font-mono">{override.reason_code}</dd>
                </div>
                <div>
                  <dt className="inline text-slate-600">By: </dt>
                  <dd className="inline font-mono">{override.overridden_by}</dd>
                </div>
                <div>
                  <dt className="inline text-slate-600">At: </dt>
                  <dd className="inline font-mono">{override.overridden_at}</dd>
                </div>
                {override.justification_uri ? (
                  <div>
                    <dt className="inline text-slate-600">Memo: </dt>
                    <dd className="inline font-mono">{override.justification_uri}</dd>
                  </div>
                ) : null}
              </dl>
              <p className="mt-3 whitespace-pre-wrap text-sm text-slate-800">
                {override.reason_text}
              </p>
            </article>
          ) : (
            <p className="rounded border border-dashed border-slate-300 bg-white p-4 text-sm text-slate-500">
              No active override. The automated decision is the system of record.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
