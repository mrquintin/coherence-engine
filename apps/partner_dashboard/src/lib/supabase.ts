import { cookies } from "next/headers";
import { createBrowserClient, createServerClient, type CookieOptions } from "@supabase/ssr";

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required env var "${name}". See apps/partner_dashboard/.env.example.`);
  }
  return value;
}

export function getSupabaseUrl(): string {
  return requireEnv("NEXT_PUBLIC_SUPABASE_URL");
}

export function getSupabaseAnonKey(): string {
  return requireEnv("NEXT_PUBLIC_SUPABASE_ANON_KEY");
}

export function createSupabaseBrowserClient() {
  return createBrowserClient(getSupabaseUrl(), getSupabaseAnonKey());
}

export function createSupabaseServerClient() {
  const cookieStore = cookies();
  return createServerClient(getSupabaseUrl(), getSupabaseAnonKey(), {
    cookies: {
      get(name: string) {
        return cookieStore.get(name)?.value;
      },
      set(name: string, value: string, options: CookieOptions) {
        try {
          cookieStore.set({ name, value, ...options });
        } catch {
          // Read-only context (e.g. RSC streaming).
        }
      },
      remove(name: string, options: CookieOptions) {
        try {
          cookieStore.set({ name, value: "", ...options });
        } catch {
          // See above.
        }
      },
    },
  });
}

export interface PartnerSession {
  accessToken: string | null;
  email: string | null;
  role: string | null;
}

export async function getServerSession(): Promise<PartnerSession> {
  try {
    const supabase = createSupabaseServerClient();
    const { data } = await supabase.auth.getUser();
    const session = await supabase.auth.getSession();
    const role =
      ((data.user?.app_metadata as Record<string, unknown> | undefined)?.role as
        | string
        | undefined) ?? null;
    return {
      accessToken: session.data.session?.access_token ?? null,
      email: data.user?.email ?? null,
      role,
    };
  } catch {
    return { accessToken: null, email: null, role: null };
  }
}

export function isPartnerOrAdmin(role: string | null): boolean {
  if (!role) return false;
  const normalized = role.toLowerCase();
  return normalized === "partner" || normalized === "admin";
}
