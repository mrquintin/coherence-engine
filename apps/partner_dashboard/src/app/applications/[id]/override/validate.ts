import type { OverrideWriteRequest } from "@/lib/api_client";

export const MIN_REASON_TEXT_LENGTH = 40;

export const VALID_REASON_CODES: ReadonlyArray<OverrideWriteRequest["reason_code"]> = [
  "factual_error",
  "policy_misalignment",
  "regulatory_constraint",
  "manual_diligence",
];

export const VALID_VERDICTS: ReadonlyArray<OverrideWriteRequest["override_verdict"]> = [
  "pass",
  "reject",
  "manual_review",
];

export interface RawOverrideForm {
  override_verdict: string;
  reason_code: string;
  reason_text: string;
  justification_uri: string;
  unrevise: boolean;
}

export type ValidateResult =
  | { ok: true; value: OverrideWriteRequest }
  | { ok: false; error: string };

export function validateOverrideForm(raw: RawOverrideForm): ValidateResult {
  if (!VALID_VERDICTS.includes(raw.override_verdict as OverrideWriteRequest["override_verdict"])) {
    return { ok: false, error: "override_verdict is required" };
  }
  if (!VALID_REASON_CODES.includes(raw.reason_code as OverrideWriteRequest["reason_code"])) {
    return { ok: false, error: "reason_code is required" };
  }
  const trimmedText = (raw.reason_text ?? "").trim();
  if (trimmedText.length < MIN_REASON_TEXT_LENGTH) {
    return {
      ok: false,
      error: `reason_text must be at least ${MIN_REASON_TEXT_LENGTH} characters`,
    };
  }
  const memo = (raw.justification_uri ?? "").trim();
  // Pass→reject memo enforcement is the backend's job too, but blocking
  // client-side avoids a round trip and makes the UI honest.
  if (raw.override_verdict === "reject" && !memo) {
    // Note: we cannot know the original verdict client-side without a
    // round trip; the server will still enforce the pass→reject memo
    // requirement. We require a memo whenever the override is reject
    // out of an abundance of caution.
    return {
      ok: false,
      error: "A memo URI is required when overriding to reject.",
    };
  }
  const value: OverrideWriteRequest = {
    override_verdict: raw.override_verdict as OverrideWriteRequest["override_verdict"],
    reason_code: raw.reason_code as OverrideWriteRequest["reason_code"],
    reason_text: trimmedText,
    justification_uri: memo || undefined,
    unrevise: raw.unrevise || undefined,
  };
  return { ok: true, value };
}
