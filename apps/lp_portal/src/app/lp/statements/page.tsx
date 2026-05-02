import Link from 'next/link';
import { requireLpSession } from '@/lib/guard';
import { formatPct, formatUsd, type LpStatementSummary } from '@/lib/lp_api';

export const dynamic = 'force-dynamic';

export default async function StatementsPage() {
  const { client, lpId } = await requireLpSession();

  let statements: LpStatementSummary[] = [];
  let errorMessage: string | null = null;
  try {
    statements = await client.listStatements();
  } catch (err) {
    errorMessage = err instanceof Error ? err.message : 'failed to load statements';
  }

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <p className="text-sm uppercase tracking-wide text-slate-500">Coherence Fund</p>
        <h1 className="text-3xl font-semibold tracking-tight">Quarterly NAV Statements</h1>
        <p className="text-sm text-slate-500">LP id <code>{lpId}</code></p>
      </header>

      <p className="text-sm text-slate-600">
        Each statement below was assembled by the Fund Administrator with
        operator-attested marks. The content digest fingerprints the rendered
        document so you can verify the file you download matches the audited
        record.
      </p>

      {errorMessage && (
        <p className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          Could not load statements: {errorMessage}
        </p>
      )}

      {statements.length === 0 && !errorMessage ? (
        <p className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-600">
          No quarterly NAV statements have been published for your account yet.
        </p>
      ) : (
        <table className="w-full overflow-hidden rounded-lg border border-slate-200 bg-white text-sm shadow-sm">
          <thead className="bg-slate-100 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3">Quarter</th>
              <th className="px-4 py-3 text-right">NAV</th>
              <th className="px-4 py-3 text-right">Cost basis (LP)</th>
              <th className="px-4 py-3 text-right">FMV (LP)</th>
              <th className="px-4 py-3 text-right">IRR</th>
              <th className="px-4 py-3">Digest</th>
              <th className="px-4 py-3 text-right">Download</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {statements.map((s) => (
              <tr key={s.statement_id}>
                <td className="px-4 py-3 font-medium text-slate-900">
                  {s.quarter_label}
                  <div className="text-xs text-slate-500">as of {s.quarter_end}</div>
                </td>
                <td className="px-4 py-3 text-right">{formatUsd(s.nav_usd)}</td>
                <td className="px-4 py-3 text-right">{formatUsd(s.total_cost_basis_usd)}</td>
                <td className="px-4 py-3 text-right">{formatUsd(s.total_fmv_usd)}</td>
                <td className="px-4 py-3 text-right">{formatPct(s.irr)}</td>
                <td className="px-4 py-3 font-mono text-xs text-slate-500">
                  {s.content_digest.slice(0, 12)}…
                </td>
                <td className="px-4 py-3 text-right">
                  <Link
                    className="text-sm font-medium text-slate-900 underline hover:text-slate-700"
                    href={s.download_url}
                  >
                    PDF
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <nav>
        <Link
          href="/lp"
          className="text-sm text-slate-500 underline hover:text-slate-700"
        >
          ← Back to overview
        </Link>
      </nav>
    </div>
  );
}
