"use client";

import { useEffect, useRef, useState } from "react";

/**
 * React hook that polls `GET /api/auth?action=application_status&application_id=…`
 * every `BASE_POLL_MS` while the application is non-terminal, with exponential
 * backoff on transient errors. Stops polling once the status enters a terminal
 * state ("decision_pass", "decision_fail", "decision_manual_review", or
 * "escalated"), or once the consumer unmounts.
 *
 * The polling cadence and backoff are intentional:
 *   - Steady-state poll: every 5 s. Decisions are issued seconds-to-minutes
 *     after the worker picks up the scoring job, so 5 s is a reasonable UX
 *     trade-off against backend load.
 *   - Errors: 5 s → 10 s → 20 s → 40 s → cap at 60 s. Resets to 5 s on the
 *     next successful poll.
 */

export interface ApplicationStatus {
  application_id: string;
  status: string;
  one_liner?: string;
  preferred_channel?: string;
  scoring_mode?: string;
  created_at?: string;
}

export interface UseApplicationStatusResult {
  status: ApplicationStatus | null;
  error: string | null;
  isPolling: boolean;
  lastPolledAt: number | null;
}

const BASE_POLL_MS = 5_000;
const MAX_BACKOFF_MS = 60_000;

const TERMINAL_STATUSES = new Set([
  "decision_pass",
  "decision_fail",
  "decision_manual_review",
  "escalated",
]);

export function isTerminalStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return TERMINAL_STATUSES.has(status);
}

export function useApplicationStatus(applicationId: string | null): UseApplicationStatusResult {
  const [status, setStatus] = useState<ApplicationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastPolledAt, setLastPolledAt] = useState<number | null>(null);
  const [isPolling, setIsPolling] = useState<boolean>(false);
  const cancelledRef = useRef(false);

  useEffect(() => {
    if (!applicationId) {
      return;
    }
    cancelledRef.current = false;
    setIsPolling(true);
    let backoffMs = BASE_POLL_MS;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (cancelledRef.current) return;
      try {
        const res = await fetch(
          `/api/auth?action=application_status&application_id=${encodeURIComponent(applicationId)}`,
          { cache: "no-store" },
        );
        if (!res.ok) {
          throw new Error(`status ${res.status}`);
        }
        const body = (await res.json()) as ApplicationStatus;
        if (cancelledRef.current) return;
        setStatus(body);
        setError(null);
        setLastPolledAt(Date.now());
        backoffMs = BASE_POLL_MS; // reset backoff on success
        if (isTerminalStatus(body.status)) {
          setIsPolling(false);
          return;
        }
        timer = setTimeout(tick, BASE_POLL_MS);
      } catch (err) {
        if (cancelledRef.current) return;
        setError(err instanceof Error ? err.message : "polling failed");
        // exponential backoff, capped
        backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
        timer = setTimeout(tick, backoffMs);
      }
    };

    tick();

    return () => {
      cancelledRef.current = true;
      setIsPolling(false);
      if (timer) {
        clearTimeout(timer);
      }
    };
  }, [applicationId]);

  return { status, error, isPolling, lastPolledAt };
}
