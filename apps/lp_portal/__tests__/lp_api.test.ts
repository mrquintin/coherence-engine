import { describe, expect, it, vi } from 'vitest';
import { LpApiClient, LpApiError, formatPct, formatUsd } from '@/lib/lp_api';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('LpApiClient', () => {
  it('stamps Authorization, Accept, and X-LP-ID headers on every request', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse(200, { items: [] }));
    const client = new LpApiClient({
      accessToken: 'token-1',
      lpId: 'lp_42',
      baseUrl: 'https://api.example.test',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await client.listStatements();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.example.test/api/v1/lp/lp_42/statements');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer token-1');
    expect(headers.Accept).toBe('application/json');
    expect(headers['X-LP-ID']).toBe('lp_42');
  });

  it('url-encodes the lp id in the path', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        lp_id: 'lp/needs+encoding',
        legal_name: 'X',
        commitment_usd: 0,
        called_to_date_usd: 0,
        uncalled_capital_usd: 0,
        distributions_to_date_usd: 0,
        current_nav_usd: 0,
        irr: null,
        latest_quarter_label: null,
      }),
    );
    const client = new LpApiClient({
      accessToken: 't',
      lpId: 'lp/needs+encoding',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await client.getOverview();
    const [url] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/lp/lp%2Fneeds%2Bencoding/overview');
  });

  it('raises LpApiError on non-2xx', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(jsonResponse(403, { error: 'forbidden' }));
    const client = new LpApiClient({
      accessToken: 't',
      lpId: 'lp_x',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await expect(client.listNotices()).rejects.toBeInstanceOf(LpApiError);
  });

  it('refuses construction without an access token or lp id', () => {
    expect(() => new LpApiClient({ accessToken: '', lpId: 'x' })).toThrow();
    expect(() => new LpApiClient({ accessToken: 'x', lpId: '' })).toThrow();
  });
});

describe('formatters', () => {
  it('formatUsd renders currency with two decimals', () => {
    expect(formatUsd(1234.5)).toBe('$1,234.50');
    expect(formatUsd(0)).toBe('$0.00');
    expect(formatUsd(null)).toBe('—');
  });

  it('formatPct renders percentages with two decimals', () => {
    expect(formatPct(0.1234)).toBe('12.34%');
    expect(formatPct(null)).toBe('n/a');
  });
});
