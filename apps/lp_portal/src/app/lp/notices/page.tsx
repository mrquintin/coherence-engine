import Link from 'next/link';
import { requireLpSession } from '@/lib/guard';
import { formatUsd, type LpNoticeSummary } from '@/lib/lp_api';

export const dynamic = 'force-dynamic';

const KIND_LABEL: Record<LpNoticeSummary['kind'], string> = {
  capital_call: 'Capital call',
  distribution: 'Distribution',
};

const STATUS_LABEL: Record<LpNoticeSummary['status'], string> = {
  pending_acknowledgement: 'Pending acknowledgement',
  acknowledged: 'Acknowledged',
  paid: 'Paid',
  cancelled: 'Cancelled',
};

export default async function NoticesPage() {
  const { client, lpId } = await requireLpSession();

  let notices: LpNoticeSummary[] = [];
  let errorMessage: string | null = null;
  try {
    notices = await client.listNotices();
  } catch (err) {
    errorMessage = err instanceof Error ? err.message : 'failed to load notices';
  }

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <p className="text-sm uppercase tracking-wide text-slate-500">Coherence Fund</p>
        <h1 className="text-3xl font-semibold tracking-tight">
          Capital Calls &amp; Distributions
        </h1>
        <p className="text-sm text-slate-500">LP id <code>{lpId}</code></p>
      </header>

      <p className="text-sm text-slate-600">
        Capital-call notices are binding under the LPA — please review and
        acknowledge each notice before its due date. Distribution notices are
        records of the Fund&rsquo;s intent to wire proceeds to your account on
        file; acknowledgement confirms receipt only. LP funding accounts are
        managed from the{' '}
        <Link className="font-medium underline" href="/lp/funding-source">
          funding-source page
        </Link>
        .
      </p>

      {errorMessage && (
        <p className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          Could not load notices: {errorMessage}
        </p>
      )}

      {notices.length === 0 && !errorMessage ? (
        <p className="rounded-md border border-slate-200 bg-white p-4 text-sm text-slate-600">
          No notices on file.
        </p>
      ) : (
        <table className="w-full overflow-hidden rounded-lg border border-slate-200 bg-white text-sm shadow-sm">
          <thead className="bg-slate-100 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3">Date</th>
              <th className="px-4 py-3">Kind</th>
              <th className="px-4 py-3 text-right">Amount</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3 text-right">Document</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {notices.map((n) => (
              <tr key={n.notice_id}>
                <td className="px-4 py-3">{n.notice_date}</td>
                <td className="px-4 py-3 font-medium text-slate-900">
                  {KIND_LABEL[n.kind]}
                </td>
                <td className="px-4 py-3 text-right">{formatUsd(n.amount_usd)}</td>
                <td className="px-4 py-3">{STATUS_LABEL[n.status]}</td>
                <td className="px-4 py-3 text-right">
                  <Link
                    className="text-sm font-medium text-slate-900 underline hover:text-slate-700"
                    href={n.download_url}
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
