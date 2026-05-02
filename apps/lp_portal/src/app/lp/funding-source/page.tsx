import Link from 'next/link';
import { FundingSourceLauncher } from '@/components/funding_source_launcher';
import { requireLpSession } from '@/lib/guard';

export const dynamic = 'force-dynamic';

export default async function FundingSourcePage() {
  const { email, lpId } = await requireLpSession();
  const provider =
    process.env.NEXT_PUBLIC_LP_BANK_LINK_PROVIDER ?? 'Hosted bank-link provider';

  return (
    <div className="space-y-7">
      <header className="space-y-1">
        <p className="text-sm uppercase tracking-wide text-slate-500">Coherence Fund</p>
        <h1 className="text-3xl font-semibold tracking-tight">Funding Source</h1>
        <p className="text-sm text-slate-500">
          Signed in as {email ?? 'unknown'} · LP id <code>{lpId}</code>
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <div className="grid gap-4 md:grid-cols-2">
          <StatusItem label="Investor verification" status="Required before funding" />
          <StatusItem label="Subscription documents" status="Required before funding" />
          <StatusItem label="Bank-link rail" status={provider} />
          <StatusItem label="Capital-call debit" status="Never automatic until authorized" />
        </div>
      </section>

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <div className="space-y-2">
          <h2 className="text-lg font-semibold text-slate-900">Connect a separate bank</h2>
          <p className="text-sm leading-6 text-slate-600">
            Use this page for the bank account that will fund LP capital calls.
            Service-provider billing accounts are intentionally separate from LP
            funding accounts.
          </p>
          <p className="text-sm leading-6 text-slate-600">
            The portal opens a hosted bank-link session. Raw account and routing
            numbers are not entered into or stored by this Next.js app.
          </p>
        </div>

        <FundingSourceLauncher />
      </section>

      <section className="rounded-lg border border-slate-200 bg-slate-100 p-5">
        <h2 className="text-base font-semibold text-slate-900">Before any transfer</h2>
        <ul className="mt-3 list-disc space-y-2 pl-5 text-sm leading-6 text-slate-600">
          <li>Investor identity, accreditation, and subscription status must be approved.</li>
          <li>Capital calls still need the notice, mandate, and payment authorization.</li>
          <li>Distributions should use the separately verified payout instructions on file.</li>
        </ul>
      </section>

      <nav className="flex flex-wrap gap-4">
        <Link href="/lp" className="text-sm text-slate-500 underline hover:text-slate-700">
          Back to overview
        </Link>
        <Link
          href="/lp/notices"
          className="text-sm text-slate-500 underline hover:text-slate-700"
        >
          Capital calls &amp; distributions
        </Link>
      </nav>
    </div>
  );
}

function StatusItem({ label, status }: { label: string; status: string }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-sm font-medium text-slate-900">{status}</p>
    </div>
  );
}
