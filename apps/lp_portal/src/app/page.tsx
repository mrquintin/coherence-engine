import Link from 'next/link';
import { createSupabaseServerClient } from '@/lib/supabase';
import { hasLpRole } from '@/lib/rbac';

export default async function HomePage() {
  let signedInEmail: string | null = null;
  let isLp = false;
  try {
    const supabase = createSupabaseServerClient();
    const { data } = await supabase.auth.getUser();
    signedInEmail = data.user?.email ?? null;
    isLp = hasLpRole(data.user ?? null);
  } catch {
    // Env not configured locally is fine — the landing page still renders.
  }

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm font-medium uppercase tracking-wide text-slate-500">
          Coherence Fund
        </p>
        <h1 className="text-4xl font-semibold tracking-tight">LP Portal</h1>
        <p className="text-lg text-slate-600">
          Quarterly NAV statements, capital-call notices, and distribution notices
          for verified limited partners.
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        {signedInEmail ? (
          <div className="space-y-4">
            <p className="text-sm text-slate-600">
              Signed in as <span className="font-medium">{signedInEmail}</span>.
            </p>
            {isLp ? (
              <Link
                href="/lp"
                className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
                data-testid="enter-lp-portal"
              >
                Enter LP portal
              </Link>
            ) : (
              <p className="text-sm text-amber-700">
                Your account does not yet carry the <code>lp</code> role. Please
                complete accredited-investor verification or contact the fund
                administrator.
              </p>
            )}
          </div>
        ) : (
          <form action="/api/auth" method="post" className="space-y-4">
            <p className="text-sm text-slate-600">
              Sign in with your verified investor email to view your statements
              and notices.
            </p>
            <button
              type="submit"
              name="action"
              value="signin"
              data-testid="signin-button"
              className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
            >
              Sign in
            </button>
          </form>
        )}
      </section>
    </div>
  );
}
