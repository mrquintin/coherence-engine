'use client';

import { useState } from 'react';

type LaunchState = 'idle' | 'starting' | 'failed';

export function FundingSourceLauncher() {
  const [state, setState] = useState<LaunchState>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function startLink() {
    setState('starting');
    setErrorMessage(null);

    try {
      const response = await fetch('/api/funding-source/start', {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
      const payload = (await response.json().catch(() => ({}))) as {
        error?: string;
        url?: string;
      };

      if (!response.ok || !payload.url) {
        throw new Error(payload.error ?? `Bank-link start failed (${response.status})`);
      }

      window.location.assign(payload.url);
    } catch (err) {
      setState('failed');
      setErrorMessage(err instanceof Error ? err.message : 'Bank-link start failed');
    }
  }

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={startLink}
        disabled={state === 'starting'}
        className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-400"
      >
        {state === 'starting' ? 'Opening bank link...' : 'Connect funding bank'}
      </button>
      {errorMessage ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          {errorMessage}
        </p>
      ) : null}
    </div>
  );
}
