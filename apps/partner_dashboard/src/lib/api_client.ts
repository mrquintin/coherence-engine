/**
 * Thin partner-API client. Server-component callers compose `createPartnerApiClient`
 * with the Supabase access token and call typed methods that hit the
 * `/api/v1/partner/*` namespace on the FastAPI backend.
 */

export interface PartnerEnvelopeMeta {
  request_id: string;
}

export interface ErrorEnvelope {
  data: null;
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>[];
  };
  meta: PartnerEnvelopeMeta;
}

export class PartnerApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId?: string;
  readonly details?: Record<string, unknown>[];

  constructor(args: {
    status: number;
    code: string;
    message: string;
    requestId?: string;
    details?: Record<string, unknown>[];
  }) {
    super(args.message);
    this.name = "PartnerApiError";
    this.status = args.status;
    this.code = args.code;
    this.requestId = args.requestId;
    this.details = args.details;
  }
}

export interface PipelineFilter {
  domain: string;
  verdict: string;
  mode: string;
}

export interface PipelineItem {
  application_id: string;
  founder_id: string;
  domain_primary: string;
  status: string;
  scoring_mode: string;
  automated_verdict: string;
  effective_verdict: string;
  override_active: boolean;
  override_reason_code: string;
  coherence_observed: number | null;
  threshold_required: number | null;
  margin: number | null;
  created_at: string;
  updated_at: string;
}

export interface PipelineResponse {
  data: {
    items: PipelineItem[];
    next_cursor: string | null;
    has_more: boolean;
    filter: PipelineFilter;
  };
  error: null;
  meta: PartnerEnvelopeMeta;
}

export interface DecisionArtifact {
  decision: string;
  policy_version: string;
  decision_policy_version: string | null;
  parameter_set_id: string;
  threshold_required: number;
  coherence_observed: number;
  margin: number;
  failed_gates: unknown[];
}

export interface OverrideRecord {
  id: string;
  override_verdict: string;
  reason_code: string;
  reason_text: string;
  justification_uri: string;
  overridden_by: string;
  overridden_at: string;
}

export interface ApplicationDetailResponse {
  data: PipelineItem & {
    decision_artifact: DecisionArtifact | null;
    override: OverrideRecord | null;
  };
  error: null;
  meta: PartnerEnvelopeMeta;
}

export interface OverrideWriteRequest {
  override_verdict: "pass" | "reject" | "manual_review";
  reason_code:
    | "factual_error"
    | "policy_misalignment"
    | "regulatory_constraint"
    | "manual_diligence";
  reason_text: string;
  justification_uri?: string;
  unrevise?: boolean;
}

export interface OverrideWriteResponse {
  data: {
    override_id: string;
    application_id: string;
    original_verdict: string;
    override_verdict: string;
    reason_code: string;
    reason_text: string;
    justification_uri: string;
    overridden_by: string;
    overridden_at: string;
    status: string;
    created: boolean;
    superseded_override_id: string;
  };
  error: null;
  meta: PartnerEnvelopeMeta;
}

export interface AuditEntry {
  id: string;
  action: string;
  success: boolean;
  actor: string;
  request_id: string;
  ip: string;
  path: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface AuditResponse {
  data: {
    items: AuditEntry[];
    filter: { application_id: string; action: string };
  };
  error: null;
  meta: PartnerEnvelopeMeta;
}

function backendUrlFromEnv(): string {
  const url = process.env.BACKEND_API_URL;
  if (!url) {
    throw new Error("Missing BACKEND_API_URL env var.");
  }
  return url.replace(/\/+$/, "");
}

export interface PartnerApiClientOptions {
  baseUrl: string;
  accessToken?: string | null;
  fetchImpl?: typeof fetch;
}

export class PartnerApiClient {
  private readonly baseUrl: string;
  private readonly accessToken: string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(options: PartnerApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.accessToken = options.accessToken ?? null;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  private buildHeaders(extra: Record<string, string> = {}): Record<string, string> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      ...extra,
    };
    if (this.accessToken) {
      headers.Authorization = `Bearer ${this.accessToken}`;
    }
    return headers;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const url = `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
    const headers = this.buildHeaders(
      body !== undefined ? { "Content-Type": "application/json" } : {},
    );
    const response = await this.fetchImpl(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });

    const text = await response.text();
    let parsed: unknown = null;
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new PartnerApiError({
          status: response.status,
          code: "invalid_response",
          message: `Backend returned non-JSON body (status ${response.status})`,
        });
      }
    }
    if (!response.ok) {
      const envelope = parsed as Partial<ErrorEnvelope> | null;
      throw new PartnerApiError({
        status: response.status,
        code: envelope?.error?.code ?? `http_${response.status}`,
        message: envelope?.error?.message ?? response.statusText ?? "Request failed",
        requestId: envelope?.meta?.request_id,
        details: envelope?.error?.details,
      });
    }
    return parsed as T;
  }

  getPipeline(
    filter: Partial<PipelineFilter> & { cursor?: string; limit?: number },
  ): Promise<PipelineResponse> {
    const params = new URLSearchParams();
    if (filter.domain) params.set("domain", filter.domain);
    if (filter.verdict) params.set("verdict", filter.verdict);
    if (filter.mode) params.set("mode", filter.mode);
    if (filter.cursor) params.set("cursor", filter.cursor);
    if (filter.limit) params.set("limit", String(filter.limit));
    const qs = params.toString();
    return this.request<PipelineResponse>("GET", `/api/v1/partner/pipeline${qs ? `?${qs}` : ""}`);
  }

  getApplication(applicationId: string): Promise<ApplicationDetailResponse> {
    return this.request<ApplicationDetailResponse>(
      "GET",
      `/api/v1/partner/applications/${encodeURIComponent(applicationId)}`,
    );
  }

  postOverride(applicationId: string, body: OverrideWriteRequest): Promise<OverrideWriteResponse> {
    return this.request<OverrideWriteResponse>(
      "POST",
      `/api/v1/partner/applications/${encodeURIComponent(applicationId)}/override`,
      body,
    );
  }

  getAudit(opts: {
    application_id?: string;
    action?: string;
    limit?: number;
  }): Promise<AuditResponse> {
    const params = new URLSearchParams();
    if (opts.application_id) params.set("application_id", opts.application_id);
    if (opts.action) params.set("action", opts.action);
    if (opts.limit) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return this.request<AuditResponse>("GET", `/api/v1/partner/audit${qs ? `?${qs}` : ""}`);
  }
}

export function createPartnerApiClient(accessToken: string | null): PartnerApiClient {
  return new PartnerApiClient({
    baseUrl: backendUrlFromEnv(),
    accessToken,
  });
}
