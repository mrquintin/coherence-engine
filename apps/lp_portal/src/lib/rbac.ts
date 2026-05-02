import type { Session, User } from '@supabase/supabase-js';

/**
 * RBAC role required to view any /lp/* page.
 *
 * Role membership is sourced from the Supabase Auth `app_metadata.roles`
 * array (set server-side by the verification flow in prompt 26). We also
 * accept a single string under `app_metadata.role` for legacy LPs whose
 * profile predates the multi-role rollout. The fund-side `partner_dashboard`
 * uses the same convention with `roles: ["partner"]`.
 */
export const LP_ROLE = 'lp' as const;

export type LpRoleCheckResult =
  | { kind: 'allowed'; user: User; session: Session; lpId: string }
  | { kind: 'unauthenticated' }
  | { kind: 'forbidden'; user: User };

/**
 * `requireLpRole` returns a discriminated union the page component can
 * branch on. We deliberately avoid throwing redirects from within this
 * helper because Next.js App Router redirects must originate from a
 * server component / route handler, not from a shared library function.
 */
export function evaluateLpRole(session: Session | null): LpRoleCheckResult {
  if (!session?.user) {
    return { kind: 'unauthenticated' };
  }
  const user = session.user;
  if (!hasLpRole(user)) {
    return { kind: 'forbidden', user };
  }
  const lpId = extractLpId(user);
  if (!lpId) {
    return { kind: 'forbidden', user };
  }
  return { kind: 'allowed', user, session, lpId };
}

export function hasLpRole(user: User | null): boolean {
  if (!user) return false;
  const meta = user.app_metadata ?? {};
  const roles = (meta as Record<string, unknown>).roles;
  if (Array.isArray(roles) && roles.includes(LP_ROLE)) {
    return true;
  }
  const role = (meta as Record<string, unknown>).role;
  return role === LP_ROLE;
}

/**
 * The LP id is stamped into Supabase Auth `app_metadata.lp_id` by the
 * accreditation flow (prompt 26) when the investor passes verification.
 * It is the foreign key the backend uses to enforce row-level security
 * on the LP statement / notice tables — no server-side filter falls back
 * to "all rows for any LP" if this is missing.
 */
export function extractLpId(user: User | null): string | null {
  if (!user) return null;
  const meta = user.app_metadata ?? {};
  const lpId = (meta as Record<string, unknown>).lp_id;
  if (typeof lpId === 'string' && lpId.length > 0) {
    return lpId;
  }
  return null;
}
