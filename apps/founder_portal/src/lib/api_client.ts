import type {
  CreateApplicationRequest,
  CreateApplicationResponse,
  EnvelopeMeta,
  ErrorEnvelope,
  GetDecisionArtifactResponse,
  GetDecisionResponse,
} from "@/sdk";

export class BackendApiError extends Error {
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
    this.name = "BackendApiError";
    this.status = args.status;
    this.code = args.code;
    this.requestId = args.requestId;
    this.details = args.details;
  }
}

export interface ApiClientOptions {
  baseUrl: string;
  accessToken?: string | null;
  fetchImpl?: typeof fetch;
  idempotencyKey?: () => string;
}

const DEFAULT_IDEMPOTENCY_KEY = () => {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

function backendUrlFromEnv(): string {
  const url = process.env.BACKEND_API_URL;
  if (!url) {
    throw new Error(
      "Missing BACKEND_API_URL. Set it in .env.local or the Vercel project settings.",
    );
  }
  return url.replace(/\/+$/, "");
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly accessToken: string | null;
  private readonly fetchImpl: typeof fetch;
  private readonly newIdempotencyKey: () => string;

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.accessToken = options.accessToken ?? null;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.newIdempotencyKey = options.idempotencyKey ?? DEFAULT_IDEMPOTENCY_KEY;
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

  private async request<T>(
    method: string,
    path: string,
    init: { body?: unknown; idempotent?: boolean } = {},
  ): Promise<T> {
    const url = `${this.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
    const headers: Record<string, string> = this.buildHeaders();
    if (init.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (init.idempotent) {
      headers["Idempotency-Key"] = this.newIdempotencyKey();
    }

    const response = await this.fetchImpl(url, {
      method,
      headers,
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      cache: "no-store",
    });

    const text = await response.text();
    let parsed: unknown = null;
    if (text.length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new BackendApiError({
          status: response.status,
          code: "invalid_response",
          message: `Backend returned non-JSON body (status ${response.status})`,
        });
      }
    }

    if (!response.ok) {
      const envelope = parsed as Partial<ErrorEnvelope> | null;
      throw new BackendApiError({
        status: response.status,
        code: envelope?.error?.code ?? `http_${response.status}`,
        message: envelope?.error?.message ?? response.statusText ?? "Request failed",
        requestId: envelope?.meta?.request_id,
        details: envelope?.error?.details,
      });
    }

    return parsed as T;
  }

  createApplication(payload: CreateApplicationRequest): Promise<CreateApplicationResponse> {
    return this.request<CreateApplicationResponse>("POST", "/api/v1/applications", {
      body: payload,
      idempotent: true,
    });
  }

  getApplication(applicationId: string): Promise<{
    data: {
      application_id: string;
      founder_id: string;
      status: string;
      one_liner?: string;
      preferred_channel?: string;
      scoring_mode?: string;
      created_at?: string;
    };
    error: null;
    meta: EnvelopeMeta;
  }> {
    return this.request("GET", `/api/v1/applications/${encodeURIComponent(applicationId)}`);
  }

  initiateUpload(
    applicationId: string,
    body: {
      filename: string;
      content_type: string;
      size_bytes: number;
      kind: "deck" | "supporting";
    },
  ): Promise<{
    data: {
      upload_id: string;
      upload_url: string;
      headers: Record<string, string>;
      expires_at: string;
      key: string;
      uri: string;
      max_bytes: number;
    };
    error: null;
    meta: EnvelopeMeta;
  }> {
    return this.request(
      "POST",
      `/api/v1/applications/${encodeURIComponent(applicationId)}/uploads:initiate`,
      { body },
    );
  }

  completeUpload(
    applicationId: string,
    upload_id: string,
  ): Promise<{
    data: {
      upload_id: string;
      uri: string;
      size_bytes: number;
      status: "completed";
    };
    error: null;
    meta: EnvelopeMeta;
  }> {
    return this.request(
      "POST",
      `/api/v1/applications/${encodeURIComponent(applicationId)}/uploads:complete`,
      { body: { upload_id } },
    );
  }

  getDecision(applicationId: string): Promise<GetDecisionResponse> {
    return this.request<GetDecisionResponse>(
      "GET",
      `/api/v1/applications/${encodeURIComponent(applicationId)}/decision`,
    );
  }

  getDecisionArtifact(applicationId: string): Promise<GetDecisionArtifactResponse> {
    return this.request<GetDecisionArtifactResponse>(
      "GET",
      `/api/v1/applications/${encodeURIComponent(applicationId)}/decision_artifact`,
    );
  }
}

export async function createServerApiClient(accessToken: string | null): Promise<ApiClient> {
  return new ApiClient({
    baseUrl: backendUrlFromEnv(),
    accessToken,
  });
}
