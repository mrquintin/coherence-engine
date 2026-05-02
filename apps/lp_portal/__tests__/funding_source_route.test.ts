import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session, User } from '@supabase/supabase-js';
import type { NextRequest } from 'next/server';

const supabaseMocks = vi.hoisted(() => ({
  getSession: vi.fn(),
}));

vi.mock('@/lib/supabase', () => ({
  createSupabaseServerClient: vi.fn(() => ({
    auth: {
      getSession: supabaseMocks.getSession,
    },
  })),
}));

import { POST } from '@/app/api/funding-source/start/route';

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

function request(): NextRequest {
  return new Request('https://portal.example.test/lp/funding-source', {
    method: 'POST',
  }) as NextRequest;
}

function mockSession(session: Session | null): void {
  supabaseMocks.getSession.mockResolvedValue({ data: { session } });
}

describe('funding-source start route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    delete process.env.LP_BANK_LINK_START_URL;
    delete process.env.NEXT_PUBLIC_LP_BANK_LINK_PROVIDER;
  });

  it('rejects unauthenticated requests', async () => {
    mockSession(null);

    const response = await POST(request());

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({
      error: 'Authentication required',
    });
  });

  it('rejects authenticated users without LP access', async () => {
    mockSession(makeSession(makeUser({ roles: ['founder'], lp_id: 'lp_42' })));

    const response = await POST(request());

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({ error: 'LP access required' });
  });

  it('returns a configured hosted bank-link URL scoped to the session LP', async () => {
    process.env.LP_BANK_LINK_START_URL =
      'https://bank-link.example.test/start?existing=1';
    process.env.NEXT_PUBLIC_LP_BANK_LINK_PROVIDER = 'Stripe Financial Connections';
    mockSession(makeSession(makeUser({ roles: ['lp'], lp_id: 'lp_42' })));

    const response = await POST(request());
    const payload = (await response.json()) as { provider: string; url: string };
    const url = new URL(payload.url);

    expect(response.status).toBe(200);
    expect(payload.provider).toBe('Stripe Financial Connections');
    expect(url.origin).toBe('https://bank-link.example.test');
    expect(url.pathname).toBe('/start');
    expect(url.searchParams.get('existing')).toBe('1');
    expect(url.searchParams.get('lp_id')).toBe('lp_42');
    expect(url.searchParams.get('flow')).toBe('lp_funding_source');
    expect(url.searchParams.get('return_url')).toBe(
      'https://portal.example.test/lp/funding-source?status=returned',
    );
  });

  it('reports the provider as unconfigured until the hosted start URL exists', async () => {
    mockSession(makeSession(makeUser({ roles: ['lp'], lp_id: 'lp_42' })));

    const response = await POST(request());
    const payload = (await response.json()) as { error: string };

    expect(response.status).toBe(501);
    expect(payload.error).toContain('Bank-link provider is not configured');
  });

  it('rejects non-https provider URLs outside localhost', async () => {
    process.env.LP_BANK_LINK_START_URL = 'http://bank-link.example.test/start';
    mockSession(makeSession(makeUser({ roles: ['lp'], lp_id: 'lp_42' })));

    const response = await POST(request());
    const payload = (await response.json()) as { error: string };

    expect(response.status).toBe(500);
    expect(payload.error).toContain('must use https');
  });
});
