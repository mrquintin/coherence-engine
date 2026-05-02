/**
 * Thin client for the LP-reporting endpoints on the fund backend.
 *
 * The LP portal does not own the data — every fetch carries the LP's
 * Supabase access token plus the resolved `lp_id` and the backend
 * enforces row-level security so an LP can never see another LP's
 * statement / notice. This module is the only place the portal talks
 * to the backend; pages and components consume the typed wrappers.
 */

export interface LpStatementSummary {
  statement_id: string;
  quarter_label: string;
  quarter_end: string;
  nav_usd: number;
  total_cost_basis_usd: number;
  total_fmv_usd: number;
  irr: number | null;
  content_digest: string;
  download_url: string;
}

export interface LpNoticeSummary {
  notice_id: string;
  kind: 'capital_call' | 'distribution';
  notice_date: string;
  amount_usd: number;
  status: 'pending_acknowledgement' | 'acknowledged' | 'paid' | 'cancelled';
  download_url: string;
}

export interface LpOverview {
  lp_id: string;
  legal_name: string;
  commitment_usd: number;
  called_to_date_usd: number;
  uncalled_capital_usd: number;
  distributions_to_date_usd: number;
  current_nav_usd: number;
  irr: number | null;
  latest_quarter_label: string | null;
}

const DEFAULT_BASE_URL =
  process.env.NEXT_PUBLIC_LP_API_BASE_URL ?? 'http://localhost:8000';

export class LpApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'LpApiError';
  }
}

export interface LpApiClientOptions {
  accessToken: string;
  lpId: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

export class LpApiClient {
  private readonly base: string;
  private readonly accessToken: string;
  private readonly lpId: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: LpApiClientOptions) {
    if (!opts.accessToken) throw new Error('LpApiClient requires accessToken');
    if (!opts.lpId) throw new Error('LpApiClient requires lpId');
    this.base = (opts.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, '');
    this.accessToken = opts.accessToken;
    this.lpId = opts.lpId;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  async getOverview(): Promise<LpOverview> {
    return this.request<LpOverview>(`/api/v1/lp/${encodeURIComponent(this.lpId)}/overview`);
  }

  async listStatements(): Promise<LpStatementSummary[]> {
    const payload = await this.request<{ items: LpStatementSummary[] }>(
      `/api/v1/lp/${encodeURIComponent(this.lpId)}/statements`,
    );
    return payload.items ?? [];
  }

  async listNotices(): Promise<LpNoticeSummary[]> {
    const payload = await this.request<{ items: LpNoticeSummary[] }>(
      `/api/v1/lp/${encodeURIComponent(this.lpId)}/notices`,
    );
    return payload.items ?? [];
  }

  private async request<T>(path: string): Promise<T> {
    const response = await this.fetchImpl(`${this.base}${path}`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${this.accessToken}`,
        Accept: 'application/json',
        'X-LP-ID': this.lpId,
      },
      cache: 'no-store',
    });
    if (!response.ok) {
      let message = `LP API ${response.status}`;
      try {
        const body = (await response.json()) as { error?: string; message?: string };
        message = body.error ?? body.message ?? message;
      } catch {
        // body was not JSON; keep the default message
      }
      throw new LpApiError(response.status, message);
    }
    return (await response.json()) as T;
  }
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
  return `${(value * 100).toFixed(2)}%`;
}
