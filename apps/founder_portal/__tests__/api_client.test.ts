import { describe, expect, it, vi } from 'vitest';
import { ApiClient, BackendApiError } from '@/lib/api_client';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('ApiClient', () => {
  it('injects Authorization and Idempotency-Key on createApplication', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(201, {
        data: { application_id: 'app_123', founder_id: 'fnd_1', status: 'received' },
        error: null,
        meta: { request_id: 'req_1' },
      }),
    );

    const client = new ApiClient({
      baseUrl: 'https://api.example.test/',
      accessToken: 'token-xyz',
      fetchImpl: fetchImpl as unknown as typeof fetch,
      idempotencyKey: () => 'idem-fixed',
    });

    const result = await client.createApplication({
      founder: {
        full_name: 'A',
        email: 'a@b.co',
        company_name: 'Acme',
        country: 'US',
      },
      startup: {
        one_liner: 'thing',
        requested_check_usd: 100000,
        use_of_funds_summary: 'team',
        preferred_channel: 'web_voice',
      },
      consent: { ai_assessment: true, recording: true, data_processing: true },
    });

    expect(result.data.application_id).toBe('app_123');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('https://api.example.test/api/v1/applications');
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer token-xyz');
    expect(headers['Idempotency-Key']).toBe('idem-fixed');
    expect(headers['Content-Type']).toBe('application/json');
    expect(init.method).toBe('POST');
  });

  it('omits Authorization when no access token is provided', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(200, {
        data: {
          application_id: 'app_1',
          decision_id: 'dec_1',
          decision: 'pending',
          policy_version: 'v1',
          threshold_required: 0.7,
          coherence_observed: 0,
          margin: 0,
          failed_gates: [],
          updated_at: '2026-04-25T00:00:00Z',
        },
        error: null,
        meta: { request_id: 'req' },
      }),
    );
    const client = new ApiClient({
      baseUrl: 'https://api.example.test',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await client.getDecision('app_1');

    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(headers['Idempotency-Key']).toBeUndefined();
  });

  it('parses error envelope into BackendApiError', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      jsonResponse(404, {
        data: null,
        error: { code: 'application_not_found', message: 'Not found', details: [] },
        meta: { request_id: 'req-404' },
      }),
    );
    const client = new ApiClient({
      baseUrl: 'https://api.example.test',
      accessToken: 't',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.getDecision('missing')).rejects.toMatchObject({
      name: 'BackendApiError',
      status: 404,
      code: 'application_not_found',
      requestId: 'req-404',
    });
  });

  it('wraps non-JSON failures with invalid_response', async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(new Response('<html>oops</html>', { status: 502 }));
    const client = new ApiClient({
      baseUrl: 'https://api.example.test',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(client.getDecision('x')).rejects.toMatchObject({
      name: 'BackendApiError',
      code: 'invalid_response',
      status: 502,
    });
  });
});
