import { NextResponse, type NextRequest } from 'next/server';
import { createSupabaseServerClient, getSupabaseUrl } from '@/lib/supabase';

export const runtime = 'nodejs';

function authPageRedirect(): string {
  const url = new URL('/auth/v1/authorize', getSupabaseUrl());
  url.searchParams.set('provider', 'email');
  return url.toString();
}

async function readFormAction(request: NextRequest): Promise<string | null> {
  try {
    const form = await request.formData();
    const action = form.get('action');
    return typeof action === 'string' ? action : null;
  } catch {
    return null;
  }
}

export async function POST(request: NextRequest) {
  const action = await readFormAction(request);
  if (action === 'signin') {
    return NextResponse.redirect(authPageRedirect(), { status: 302 });
  }
  if (action === 'signout') {
    const supabase = createSupabaseServerClient();
    await supabase.auth.signOut();
    return NextResponse.redirect(new URL('/', request.url), { status: 302 });
  }
  return NextResponse.json({ error: 'Unknown action' }, { status: 400 });
}

export async function GET() {
  return NextResponse.redirect(authPageRedirect(), { status: 302 });
}
