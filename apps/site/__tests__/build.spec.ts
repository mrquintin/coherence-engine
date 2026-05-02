// Static-build verification + Lighthouse perf budget.
//
// Two tests:
//   1) `astro build` succeeds and produces every required HTML page.
//   2) Lighthouse (lazy-installed via `pnpm dlx`) over the locally-served
//      static `dist/` reports Performance >= 90 and Accessibility >= 95.
//
// Lighthouse is opt-in via `RUN_LIGHTHOUSE=1` so local `pnpm test` stays fast.
// CI sets `RUN_LIGHTHOUSE=1`.
import { execSync, spawn, type ChildProcess } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';

const ROOT = join(__dirname, '..');
const DIST = join(ROOT, 'dist');
const PERF_BUDGET = 90;
const A11Y_BUDGET = 95;
const PORT = 4322;
const HOST = '127.0.0.1';
const LOCAL_URL = `http://${HOST}:${PORT}`;

const REQUIRED_HTML = [
  'index.html',
  'fund/index.html',
  'results/index.html',
  'contact/index.html',
  'research/index.html',
  'research/cosine_paradox/index.html',
  'research/contradiction_direction/index.html',
  'research/reverse_marxism/index.html',
  'research/decision_policy_v1/index.html',
  'research/rss.xml',
];

describe('astro build', () => {
  beforeAll(() => {
    execSync('pnpm build', {
      cwd: ROOT,
      stdio: 'inherit',
      env: { ...process.env, SITE_URL: `http://localhost:${PORT}` },
    });
  }, 180_000);

  it('produces dist/', () => {
    expect(existsSync(DIST)).toBe(true);
  });

  for (const file of REQUIRED_HTML) {
    it(`emits ${file}`, () => {
      expect(existsSync(join(DIST, file))).toBe(true);
    });
  }

  it('every research HTML page contains the predictive-validity caveat', () => {
    const pages = REQUIRED_HTML.filter(
      (p) => p.startsWith('research/') && p.endsWith('index.html') && p !== 'research/index.html',
    );
    for (const p of pages) {
      const html = readFileSync(join(DIST, p), 'utf8');
      expect(html, `${p} missing caveat`).toContain('Predictive validity unproven');
    }
  });

  it('every page has canonical + og:title meta tags', () => {
    const pages = REQUIRED_HTML.filter((p) => p.endsWith('index.html'));
    for (const p of pages) {
      const html = readFileSync(join(DIST, p), 'utf8');
      expect(html, `${p} missing canonical`).toMatch(/rel="canonical"/);
      expect(html, `${p} missing og:title`).toMatch(/property="og:title"/);
    }
  });
});

describe.skipIf(process.env.RUN_LIGHTHOUSE !== '1')('lighthouse perf budget', () => {
  let server: ChildProcess;

  beforeAll(async () => {
    server = spawn('pnpm', ['exec', 'astro', 'preview', '--host', HOST, '--port', String(PORT)], {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${LOCAL_URL}/`);
        if (res.ok) {
          return;
        }
      } catch {
        // The preview process has not bound the socket yet.
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`preview did not respond at ${LOCAL_URL}`);
  }, 60_000);

  afterAll(() => {
    server?.kill();
  });

  it('home page meets perf >= 90 and a11y >= 95', () => {
    const out = execSync(
      `pnpm dlx lighthouse ${LOCAL_URL}/ ` +
        `--only-categories=performance,accessibility ` +
        `--chrome-flags="--headless=new --no-sandbox --disable-features=HttpsFirstBalancedModeAutoEnable,HttpsFirstModeV2,HttpsUpgrades" ` +
        `--output=json --quiet`,
      { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 },
    );
    const report = JSON.parse(out);
    const perf = Math.round((report.categories.performance.score ?? 0) * 100);
    const a11y = Math.round((report.categories.accessibility.score ?? 0) * 100);
    expect(perf, `Performance ${perf} < ${PERF_BUDGET}`).toBeGreaterThanOrEqual(PERF_BUDGET);
    expect(a11y, `Accessibility ${a11y} < ${A11Y_BUDGET}`).toBeGreaterThanOrEqual(A11Y_BUDGET);
  }, 120_000);
});
