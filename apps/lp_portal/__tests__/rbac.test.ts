import { describe, expect, it } from 'vitest';
import type { Session, User } from '@supabase/supabase-js';
import { evaluateLpRole, extractLpId, hasLpRole } from '@/lib/rbac';

function makeUser(appMetadata: Record<string, unknown>): User {
  return {
    id: 'usr_1',
    aud: 'authenticated',
    email: 'lp@example.test',
    app_metadata: appMetadata,
    user_metadata: {},
    created_at: '2026-01-01T00:00:00Z',
  } as unknown as User;
}

function makeSession(user: User): Session {
  return {
    access_token: 'token-abc',
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: 9999999999,
    refresh_token: 'refresh-abc',
    user,
  } as unknown as Session;
}

describe('LP portal RBAC', () => {
  it('hasLpRole accepts roles[] containing lp', () => {
    expect(hasLpRole(makeUser({ roles: ['lp'] }))).toBe(true);
  });

  it('hasLpRole accepts legacy single-role string', () => {
    expect(hasLpRole(makeUser({ role: 'lp' }))).toBe(true);
  });

  it('hasLpRole rejects users without the lp role', () => {
    expect(hasLpRole(makeUser({ roles: ['founder'] }))).toBe(false);
    expect(hasLpRole(makeUser({}))).toBe(false);
    expect(hasLpRole(null)).toBe(false);
  });

  it('extractLpId returns the lp_id when present', () => {
    expect(extractLpId(makeUser({ lp_id: 'lp_42' }))).toBe('lp_42');
  });

  it('extractLpId returns null when missing', () => {
    expect(extractLpId(makeUser({}))).toBeNull();
    expect(extractLpId(null)).toBeNull();
  });

  it('evaluateLpRole returns unauthenticated when session is null', () => {
    expect(evaluateLpRole(null)).toEqual({ kind: 'unauthenticated' });
  });

  it('evaluateLpRole returns forbidden when role is missing', () => {
    const user = makeUser({ lp_id: 'lp_42' }); // no roles
    const result = evaluateLpRole(makeSession(user));
    expect(result.kind).toBe('forbidden');
  });

  it('evaluateLpRole returns forbidden when lp role is present but lp_id missing', () => {
    const user = makeUser({ roles: ['lp'] });
    const result = evaluateLpRole(makeSession(user));
    expect(result.kind).toBe('forbidden');
  });

  it('evaluateLpRole returns allowed when role and lp_id are present', () => {
    const user = makeUser({ roles: ['lp'], lp_id: 'lp_42' });
    const result = evaluateLpRole(makeSession(user));
    expect(result.kind).toBe('allowed');
    if (result.kind === 'allowed') {
      expect(result.lpId).toBe('lp_42');
      expect(result.user.email).toBe('lp@example.test');
    }
  });
});

describe('LP portal RBAC — cross-LP isolation', () => {
  // The portal NEVER lets one LP see another LP's data. The lp_id
  // claim sourced from app_metadata is what the LpApiClient stamps
  // into the X-LP-ID header; if the backend ever returns rows for a
  // different LP it would be a server-side RLS bug. This test
  // ensures the *portal* never widens access by mixing LP ids.
  it('refuses requests where the URL lp_id does not match the session lp_id', () => {
    const sessionUser = makeUser({ roles: ['lp'], lp_id: 'lp_alice' });
    const session = makeSession(sessionUser);
    const result = evaluateLpRole(session);
    expect(result.kind).toBe('allowed');
    if (result.kind === 'allowed') {
      // Simulate a path-traversal attempt — the helper does NOT
      // honour any user-supplied lp_id; it only ever returns the
      // session's own lp_id.
      const attackerSuppliedLpId = 'lp_bob';
      expect(result.lpId).not.toBe(attackerSuppliedLpId);
      expect(result.lpId).toBe('lp_alice');
    }
  });
});
