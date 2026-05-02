import { createPartnerApiClient, PartnerApiError } from "@/lib/api_client";
import { getServerSession, isPartnerOrAdmin } from "@/lib/supabase";

export const dynamic = "force-dynamic";

interface AuditPageProps {
  searchParams?: Record<string, string | string[] | undefined>;
}

function pickFirst(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return (value ?? "").trim();
}

export default async function AuditPage({ searchParams }: AuditPageProps) {
  const session = await getServerSession();
  if (!isPartnerOrAdmin(session.role)) {
    return (
      <section className="rounded border border-amber-300 bg-amber-50 p-6">
        <h1 className="text-2xl font-semibold">Access required</h1>
        <p className="mt-2 text-sm text-slate-700">
          You need a <code>partner</code> or <code>admin</code> role.
        </p>
      </section>
    );
  }

  const filterAction = pickFirst(searchParams?.action);
  const filterApp = pickFirst(searchParams?.application_id);

  let items: Awaited<ReturnType<typeof fetchAudit>>["items"] = [];
  let errorMessage: string | null = null;
  try {
    const result = await fetchAudit(session.accessToken, {
      action: filterAction,
      application_id: filterApp,
    });
    items = result.items;
  } catch (err) {
    errorMessage =
      err instanceof PartnerApiError
        ? `${err.status} ${err.code}: ${err.message}`
        : (err as Error).message;
  }

  return (
    <section className="space-y-6">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">Audit log</h1>
        <p className="text-sm text-slate-600">
          Recent partner / admin actions. Filter by action name or application id.
        </p>
      </header>

      <form
        method="get"
        action="/audit"
        className="grid grid-cols-1 gap-3 rounded border border-slate-200 bg-white p-4 sm:grid-cols-3"
      >
        <label className="flex flex-col text-xs font-medium text-slate-600">
          Action
          <input
            type="text"
            name="action"
            defaultValue={filterAction}
            placeholder="decision_override_applied"
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="flex flex-col text-xs font-medium text-slate-600">
          Application
          <input
            type="text"
            name="application_id"
            defaultValue={filterApp}
            placeholder="app_..."
            className="mt-1 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <div className="flex items-end">
          <button
            type="submit"
            className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
          >
            Apply
          </button>
        </div>
      </form>

      {errorMessage ? (
        <p className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          {errorMessage}
        </p>
      ) : null}

      <div className="overflow-x-auto rounded border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-50 text-xs uppercase text-slate-600">
            <tr>
              <th className="px-3 py-2">When</th>
              <th className="px-3 py-2">Action</th>
              <th className="px-3 py-2">Outcome</th>
              <th className="px-3 py-2">Actor</th>
              <th className="px-3 py-2">Path</th>
              <th className="px-3 py-2">Details</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                  No audit events match this filter.
                </td>
              </tr>
            ) : (
              items.map((entry) => (
                <tr key={entry.id} className="border-t border-slate-100 align-top">
                  <td className="px-3 py-2 font-mono text-xs">{entry.created_at.slice(0, 19)}</td>
                  <td className="px-3 py-2 font-mono text-xs">{entry.action}</td>
                  <td className="px-3 py-2 text-xs">{entry.success ? "allowed" : "denied"}</td>
                  <td className="px-3 py-2 font-mono text-xs">{entry.actor}</td>
                  <td className="px-3 py-2 font-mono text-xs">{entry.path}</td>
                  <td className="px-3 py-2">
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs text-slate-700">
                      {JSON.stringify(entry.details, null, 2)}
                    </pre>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

async function fetchAudit(
  accessToken: string | null,
  filter: { action: string; application_id: string },
) {
  const client = createPartnerApiClient(accessToken);
  const response = await client.getAudit({
    action: filter.action || undefined,
    application_id: filter.application_id || undefined,
    limit: 50,
  });
  return response.data;
}
