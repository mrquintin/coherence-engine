import { redirect } from 'next/navigation';
import { createSupabaseServerClient } from '@/lib/supabase';
import { evaluateLpRole, type LpRoleCheckResult } from '@/lib/rbac';
import { LpApiClient } from '@/lib/lp_api';

/**
 * Server-component helper used by every /lp/* page.
 *
 * Returns the resolved (LP, accessToken) pair when the caller is allowed,
 * or `redirect()`s to the landing page when they are not. This is a
 * BLOCKING gate — there is no read-only fallback path. RLS on the backend
 * still enforces the cross-LP boundary; this helper exists so an
 * unauthenticated browser hits a 302 rather than a 403 from an empty page.
 */
export async function requireLpSession(): Promise<{
  client: LpApiClient;
  lpId: string;
  email: string | null;
  accessToken: string;
}> {
  const supabase = createSupabaseServerClient();
  const { data } = await supabase.auth.getSession();
  const result: LpRoleCheckResult = evaluateLpRole(data.session ?? null);

  if (result.kind === 'unauthenticated') {
    redirect('/');
  }
  if (result.kind === 'forbidden') {
    redirect('/?reason=forbidden');
  }

  const accessToken = result.session.access_token;
  return {
    client: new LpApiClient({
      accessToken,
      lpId: result.lpId,
    }),
    lpId: result.lpId,
    email: result.user.email ?? null,
    accessToken,
  };
}
