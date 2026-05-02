import { NextResponse, type NextRequest } from 'next/server';
import { evaluateLpRole } from '@/lib/rbac';
import { createSupabaseServerClient } from '@/lib/supabase';

export const runtime = 'nodejs';

function isLocalhost(url: URL): boolean {
  return url.hostname === 'localhost' || url.hostname === '127.0.0.1';
}

function buildFundingSourceUrl(request: NextRequest, lpId: string): URL | null {
  const configuredUrl = process.env.LP_BANK_LINK_START_URL;
  if (!configuredUrl) {
    return null;
  }

  const url = new URL(configuredUrl);
  if (url.protocol !== 'https:' && !isLocalhost(url)) {
    throw new Error('LP_BANK_LINK_START_URL must use https outside localhost');
  }

  url.searchParams.set('lp_id', lpId);
  url.searchParams.set(
    'return_url',
    new URL('/lp/funding-source?status=returned', request.url).toString(),
  );
  url.searchParams.set('flow', 'lp_funding_source');
  return url;
}

export async function POST(request: NextRequest) {
  const supabase = createSupabaseServerClient();
  const { data } = await supabase.auth.getSession();
  const result = evaluateLpRole(data.session ?? null);

  if (result.kind === 'unauthenticated') {
    return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  }
  if (result.kind === 'forbidden') {
    return NextResponse.json({ error: 'LP access required' }, { status: 403 });
  }

  let url: URL | null = null;
  try {
    url = buildFundingSourceUrl(request, result.lpId);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Invalid bank-link configuration';
    return NextResponse.json({ error: message }, { status: 500 });
  }

  if (!url) {
    return NextResponse.json(
      {
        error:
          'Bank-link provider is not configured. Set LP_BANK_LINK_START_URL to a hosted provider or backend session endpoint.',
      },
      { status: 501 },
    );
  }

  return NextResponse.json({
    provider: process.env.NEXT_PUBLIC_LP_BANK_LINK_PROVIDER ?? 'hosted_bank_link',
    url: url.toString(),
  });
}
