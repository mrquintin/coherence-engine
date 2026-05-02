// AUTO-GENERATED stub kept under version control so `tsc` / `next build` /
// `vitest` succeed without first running `python ../../scripts/generate_ts_sdk.py`.
//
// Run `pnpm generate:sdk` to regenerate the full client from
// `docs/specs/openapi_v1.yaml` (replaces every file in this directory except
// for this header note). The runtime contract below mirrors what
// `openapi-typescript-codegen` emits for the schemas we currently consume
// from the founder portal.

export interface EnvelopeMeta {
  request_id: string;
}

export interface ErrorObject {
  code: string;
  message: string;
  details?: Record<string, unknown>[];
}

export interface ErrorEnvelope {
  data: null;
  error: ErrorObject;
  meta: EnvelopeMeta;
}

export interface FounderInput {
  full_name: string;
  email: string;
  company_name: string;
  country: string;
}

export interface StartupInput {
  one_liner: string;
  requested_check_usd: number;
  use_of_funds_summary: string;
  preferred_channel: 'phone' | 'web_voice' | 'async_voice';
}

export interface ConsentInput {
  ai_assessment: boolean;
  recording: boolean;
  data_processing: boolean;
}

export interface CreateApplicationRequest {
  founder: FounderInput;
  startup: StartupInput;
  consent: ConsentInput;
}

export interface CreateApplicationData {
  application_id: string;
  founder_id: string;
  status: string;
}

export interface CreateApplicationResponse {
  data: CreateApplicationData;
  error: null;
  meta: EnvelopeMeta;
}

export interface FailedGate {
  gate: string;
  reason_code: string;
}

export type DecisionVerdict = 'pass' | 'fail' | 'manual_review' | 'pending';

export interface GetDecisionData {
  application_id: string;
  decision_id: string;
  decision: DecisionVerdict;
  policy_version: string;
  threshold_required: number;
  coherence_observed: number;
  margin: number;
  failed_gates: FailedGate[];
  updated_at: string;
}

export interface GetDecisionResponse {
  data: GetDecisionData;
  error: null;
  meta: EnvelopeMeta;
}

export interface GetDecisionArtifactData {
  application_id: string;
  artifact_id: string;
  kind: 'decision_artifact';
  decision_policy_version?: string;
  created_at?: string;
  payload: Record<string, unknown>;
}

export interface GetDecisionArtifactResponse {
  data: GetDecisionArtifactData;
  error: null;
  meta: EnvelopeMeta;
}
