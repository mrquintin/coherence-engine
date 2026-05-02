import Link from 'next/link';
import { requireLpSession } from '@/lib/guard';
import { formatPct, formatUsd, type LpOverview } from '@/lib/lp_api';

export const dynamic = 'force-dynamic';

export default async function LpHomePage() {
  const { client, email, lpId } = await requireLpSession();

  let overview: LpOverview | null = null;
  let errorMessage: string | null = null;
  try {
    overview = await client.getOverview();
  } catch (err) {
    errorMessage = err instanceof Error ? err.message : 'failed to load overview';
  }

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="text-sm uppercase tracking-wide text-slate-500">
          Coherence Fund LP Portal
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">
          Your Position Overview
        </h1>
        <p className="text-sm text-slate-500">
          Signed in as {email ?? 'unknown'} · LP id <code>{lpId}</code>
        </p>
      </header>

      {errorMessage ? (
        <p className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          Could not load your overview: {errorMessage}
        </p>
      ) : overview ? (
        <section className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <SummaryCard label="Commitment" value={formatUsd(overview.commitment_usd)} />
          <SummaryCard label="Called to date" value={formatUsd(overview.called_to_date_usd)} />
          <SummaryCard label="Uncalled" value={formatUsd(overview.uncalled_capital_usd)} />
          <SummaryCard
            label="Distributions to date"
            value={formatUsd(overview.distributions_to_date_usd)}
          />
          <SummaryCard
            label={`Current NAV${overview.latest_quarter_label ? ` (${overview.latest_quarter_label})` : ''}`}
            value={formatUsd(overview.current_nav_usd)}
          />
          <SummaryCard label="IRR (since inception)" value={formatPct(overview.irr)} />
        </section>
      ) : (
        <p className="text-sm text-slate-500">No overview data available yet.</p>
      )}

      <nav className="flex flex-wrap gap-4">
        <Link
          href="/lp/statements"
          className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
        >
          Quarterly statements
        </Link>
        <Link
          href="/lp/notices"
          className="inline-flex items-center rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-900 hover:bg-slate-100"
        >
          Capital calls &amp; distributions
        </Link>
        <Link
          href="/lp/funding-source"
          className="inline-flex items-center rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-900 hover:bg-slate-100"
        >
          Funding source
        </Link>
        <form action="/api/auth" method="post" className="ml-auto">
          <button
            type="submit"
            name="action"
            value="signout"
            className="text-sm text-slate-500 underline hover:text-slate-700"
          >
            Sign out
          </button>
        </form>
      </nav>
    </div>
  );
}

function SummaryCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-slate-900">{value}</p>
    </div>
  );
}
