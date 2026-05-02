import Link from "next/link";
import { createSupabaseServerClient } from "@/lib/supabase";

export default async function HomePage() {
  let signedInEmail: string | null = null;
  try {
    const supabase = createSupabaseServerClient();
    const { data } = await supabase.auth.getUser();
    signedInEmail = data.user?.email ?? null;
  } catch {
    // Env not configured locally is fine — the landing page still renders.
  }

  return (
    <div className="space-y-8">
      <header className="space-y-3">
        <p className="text-sm font-medium uppercase tracking-wide text-slate-500">Coherence Fund</p>
        <h1 className="text-4xl font-semibold tracking-tight">Founder Portal</h1>
        <p className="text-lg text-slate-600">
          Submit your pre-seed application and track its decision in one place.
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        {signedInEmail ? (
          <div className="space-y-4">
            <p className="text-sm text-slate-600">
              Signed in as <span className="font-medium">{signedInEmail}</span>.
            </p>
            <Link
              href="/apply"
              className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
            >
              Start an application
            </Link>
          </div>
        ) : (
          <form action="/api/auth" method="post" className="space-y-4">
            <p className="text-sm text-slate-600">
              Sign in to start a new application or check the status of an existing one.
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
