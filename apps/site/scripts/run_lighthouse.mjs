#!/usr/bin/env node
// Standalone Lighthouse runner. Lazy-installs via `pnpm dlx`. Used by CI when
// the vitest perf-budget guard isn't enough (e.g. ad-hoc local checks).
//
// Usage:
//   node scripts/run_lighthouse.mjs [url]
//
// Defaults: url = http://localhost:4321/
// Exit codes: 0 if Performance >= 90 and Accessibility >= 95, 1 otherwise.
import { execSync } from 'node:child_process';

const url = process.argv[2] ?? 'http://localhost:4321/';
const PERF = 90;
const A11Y = 95;

const out = execSync(
  `pnpm dlx lighthouse ${url} --only-categories=performance,accessibility ` +
    `--chrome-flags="--headless=new --no-sandbox" --output=json --quiet`,
  { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 },
);
const report = JSON.parse(out);
const perf = Math.round((report.categories.performance.score ?? 0) * 100);
const a11y = Math.round((report.categories.accessibility.score ?? 0) * 100);

const ok = perf >= PERF && a11y >= A11Y;
const status = ok ? 'PASS' : 'FAIL';
process.stdout.write(
  `${status} url=${url} performance=${perf} accessibility=${a11y} budgets=perf>=${PERF},a11y>=${A11Y}\n`,
);
process.exit(ok ? 0 : 1);
