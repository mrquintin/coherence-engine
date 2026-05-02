/**
 * URL-param parsing for the pipeline view.
 *
 * The pipeline page is server-side rendered with filters as query
 * parameters so a refresh / shared link reproduces the exact view.
 * The set of legal values is closed and intentionally narrow.
 */

export const KNOWN_VERDICTS = ["pass", "reject", "manual_review"] as const;
export const KNOWN_MODES = ["enforce", "shadow"] as const;

export type Verdict = (typeof KNOWN_VERDICTS)[number];
export type Mode = (typeof KNOWN_MODES)[number];

export interface PipelineFilter {
  domain: string;
  verdict: Verdict | "";
  mode: Mode | "";
  cursor: string;
  limit: number;
}

const DEFAULT_LIMIT = 25;
const MAX_LIMIT = 100;

function pickFirst(value: string | string[] | undefined): string {
  if (Array.isArray(value)) return value[0] ?? "";
  return (value ?? "").trim();
}

export function parsePipelineFilter(
  searchParams: Record<string, string | string[] | undefined> = {},
): PipelineFilter {
  const domain = pickFirst(searchParams.domain);
  const rawVerdict = pickFirst(searchParams.verdict).toLowerCase();
  const rawMode = pickFirst(searchParams.mode).toLowerCase();
  const cursor = pickFirst(searchParams.cursor);
  const rawLimit = pickFirst(searchParams.limit);

  const verdict = (KNOWN_VERDICTS as readonly string[]).includes(rawVerdict)
    ? (rawVerdict as Verdict)
    : "";
  const mode = (KNOWN_MODES as readonly string[]).includes(rawMode) ? (rawMode as Mode) : "";

  let limit = DEFAULT_LIMIT;
  const parsed = Number.parseInt(rawLimit, 10);
  if (!Number.isNaN(parsed) && parsed > 0) {
    limit = Math.min(parsed, MAX_LIMIT);
  }

  return { domain, verdict, mode, cursor, limit };
}

export function serializePipelineFilter(filter: Partial<PipelineFilter>): string {
  const params = new URLSearchParams();
  if (filter.domain) params.set("domain", filter.domain);
  if (filter.verdict) params.set("verdict", filter.verdict);
  if (filter.mode) params.set("mode", filter.mode);
  if (filter.cursor) params.set("cursor", filter.cursor);
  if (filter.limit && filter.limit !== DEFAULT_LIMIT) {
    params.set("limit", String(filter.limit));
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}
