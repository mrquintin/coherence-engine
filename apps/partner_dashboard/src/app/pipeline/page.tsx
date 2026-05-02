import Link from "next/link";
import { createPartnerApiClient, PartnerApiError } from "@/lib/api_client";
import {
  KNOWN_MODES,
  KNOWN_VERDICTS,
  parsePipelineFilter,
  serializePipelineFilter,
} from "@/lib/pipeline_filter";
import { getServerSession, isPartnerOrAdmin } from "@/lib/supabase";

export const dynamic = "force-dynamic";

interface PipelinePageProps {
  searchParams?: Record<string, string | string[] | undefined>;
}

/**
 * Pipeline pivot table.
 *
 * Filters (`pipeline|filter` markers): ``domain``, ``verdict``, ``mode``.
 * Filters live in the URL so refresh + share-links reproduce the view.
 * Pagination is cursor-based — the backend returns ``next_cursor`` and
 * we render a "Next page" link that carries the cursor forward.
 */
export default async function PipelinePage({ searchParams }: PipelinePageProps) {
  const filter = parsePipelineFilter(searchParams ?? {});
  const session = await getServerSession();

  if (!isPartnerOrAdmin(session.role)) {
    return (
      <section className="rounded border border-amber-300 bg-amber-50 p-6">
        <h1 className="text-2xl font-semibold">Access required</h1>
        <p className="mt-2 text-sm text-slate-700">
          You need a <code>partner</code> or <code>admin</code> role to view the pipeline. Contact
          the fund operator to request access.
        </p>
      </section>
    );
  }

  let items: Awaited<ReturnType<typeof fetchPipeline>>["items"] = [];
  let hasMore = false;
  let nextCursor: string | null = null;
  let errorMessage: string | null = null;
  try {
    const result = await fetchPipeline(session.accessToken, filter);
    items = result.items;
    hasMore = result.has_more;
    nextCursor = result.next_cursor;
  } catch (err) {
    errorMessage =
      err instanceof PartnerApiError
        ? `${err.status} ${err.code}: ${err.message}`
        : (err as Error).message;
  }

  return (
    <section className="space-y-6">
      <header className="flex items-baseline justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Pipeline</h1>
          <p className="text-sm text-slate-600">
            Filter applications by domain, verdict, and scoring mode.
          </p>
        </div>
        {session.email ? (
          <p className="text-sm text-slate-500">
            Signed in as <span className="font-medium">{session.email}</span> ({session.role})
          </p>
        ) : null}
      </header>

      <form
        method="get"
        action="/pipeline"
        className="grid grid-cols-1 gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-4"
        data-testid="pipeline-filter-form"
      >
        <label className="flex flex-col text-xs font-medium text-slate-600">
          Domain
          <input
            type="text"
            name="domain"
            defaultValue={filter.domain}
            placeholder="market_economics"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="flex flex-col text-xs font-medium text-slate-600">
          Verdict
          <select
            name="verdict"
            defaultValue={filter.verdict}
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">Any</option>
            {KNOWN_VERDICTS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col text-xs font-medium text-slate-600">
          Mode
          <select
            name="mode"
            defaultValue={filter.mode}
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="">Any</option>
            {KNOWN_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <div className="flex items-end gap-2">
          <button
            type="submit"
            className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          >
            Apply
          </button>
          <Link
            href="/pipeline"
            className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700"
          >
            Reset
          </Link>
        </div>
      </form>

      {errorMessage ? (
        <p className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {errorMessage}
        </p>
      ) : null}

      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm" data-testid="pipeline-table">
          <thead className="bg-slate-50 text-xs uppercase text-slate-600">
            <tr>
              <th className="px-3 py-2">Application</th>
              <th className="px-3 py-2">Domain</th>
              <th className="px-3 py-2">Mode</th>
              <th className="px-3 py-2">Auto verdict</th>
              <th className="px-3 py-2">Effective</th>
              <th className="px-3 py-2">Coherence</th>
              <th className="px-3 py-2">Override</th>
              <th className="px-3 py-2">Updated</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-slate-500">
                  No applications match this filter.
                </td>
              </tr>
            ) : (
              items.map((item) => (
                <tr
                  key={item.application_id}
                  className="border-t border-slate-100"
                  data-app-id={item.application_id}
                >
                  <td className="px-3 py-2">
                    <Link
                      href={`/applications/${item.application_id}`}
                      className="font-mono text-xs text-blue-700 hover:underline"
                    >
                      {item.application_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2">{item.domain_primary || "—"}</td>
                  <td className="px-3 py-2">{item.scoring_mode}</td>
                  <td className="px-3 py-2">{item.automated_verdict || "—"}</td>
                  <td className="px-3 py-2">
                    <span
                      className={
                        item.override_active
                          ? "rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900"
                          : "text-slate-700"
                      }
                    >
                      {item.effective_verdict || "—"}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {item.coherence_observed?.toFixed(3) ?? "—"}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {item.override_active ? item.override_reason_code : "—"}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">{item.updated_at.slice(0, 19)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="flex justify-end">
        {hasMore && nextCursor ? (
          <Link
            href={`/pipeline${serializePipelineFilter({
              ...filter,
              cursor: nextCursor,
            })}`}
            className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100"
            data-testid="pipeline-next-page"
          >
            Next page →
          </Link>
        ) : null}
      </div>
    </section>
  );
}

async function fetchPipeline(
  accessToken: string | null,
  filter: ReturnType<typeof parsePipelineFilter>,
) {
  const client = createPartnerApiClient(accessToken);
  const response = await client.getPipeline({
    domain: filter.domain,
    verdict: filter.verdict,
    mode: filter.mode,
    cursor: filter.cursor || undefined,
    limit: filter.limit,
  });
  return response.data;
}
