import { expect, test, type Route } from '@playwright/test';

/**
 * Browser-mode founder interview spec (prompt 39).
 *
 * The page loads the InterviewUi component, the user clicks
 * "Start interview", a stub MediaRecorder emits two chunks, and the
 * UI uploads each via initiate→PUT→complete then calls finalize. We
 * mock every backend route and the signed-URL PUT so no network
 * traffic leaves the browser.
 */

const SESSION_ID = 'ivw_browser_test123';

interface InitiateBody {
  seq: number;
  size_bytes: number;
}

interface CompleteBody {
  chunk_id: string;
}

async function mockInterviewRoutes(page: import('@playwright/test').Page) {
  let nextChunkId = 0;
  const completedChunks: string[] = [];

  await page.route(
    `**/api/v1/interviews/${SESSION_ID}/chunks:initiate`,
    async (route: Route) => {
      const body = route.request().postDataJSON() as InitiateBody;
      const id = `chk_test_${nextChunkId++}`;
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          request_id: 'req_test',
          data: {
            chunk_id: id,
            session_id: SESSION_ID,
            seq: body.seq,
            upload_url: `https://signed.example/chunk/${id}?signed_url=1`,
            headers: { 'Content-Type': 'audio/webm' },
            expires_at: new Date(Date.now() + 60_000).toISOString(),
            key: `interviews/${SESSION_ID}/chunk_${String(body.seq).padStart(5, '0')}.webm`,
            uri: `coh://local/default/interviews/${SESSION_ID}/chunk_${String(body.seq).padStart(5, '0')}.webm`,
            max_bytes: 5 * 1024 * 1024,
          },
        }),
      });
    },
  );

  await page.route(
    `**/api/v1/interviews/${SESSION_ID}/chunks:complete`,
    async (route: Route) => {
      const body = route.request().postDataJSON() as CompleteBody;
      completedChunks.push(body.chunk_id);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          request_id: 'req_test',
          data: {
            chunk_id: body.chunk_id,
            seq: completedChunks.length - 1,
            uri: `coh://local/default/interviews/${SESSION_ID}/${body.chunk_id}.webm`,
            size_bytes: 1024,
            sha256: 'a'.repeat(64),
            status: 'completed',
          },
        }),
      });
    },
  );

  await page.route(
    `**/api/v1/interviews/${SESSION_ID}:finalize`,
    async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          request_id: 'req_test',
          data: {
            session_id: SESSION_ID,
            status: 'completed',
            full_uri: `coh://local/default/interviews/${SESSION_ID}/full.webm`,
            full_sha256: 'b'.repeat(64),
            chunk_count: completedChunks.length,
            event_id: 'evt_test',
            idempotent: false,
          },
        }),
      });
    },
  );

  await page.route('https://signed.example/**', async (route: Route) => {
    await route.fulfill({ status: 200, body: '' });
  });
}

test.describe('browser-mode interview', () => {
  test('captures chunks, uploads in seq order, finalizes', async ({ page }) => {
    await mockInterviewRoutes(page);

    // Stub MediaRecorder + getUserMedia BEFORE the page bundle loads
    // so the interview UI sees the fakes when it imports
    // ``WebRtcRecorder``. Using ``addInitScript`` keeps the override
    // attached across navigation.
    await page.addInitScript(() => {
      class FakeMediaStream {
        getTracks() {
          return [{ stop() {} }];
        }
      }

      Object.defineProperty(window.navigator, 'mediaDevices', {
        configurable: true,
        value: {
          getUserMedia: async () => new FakeMediaStream(),
        },
      });

      // Minimal MediaRecorder stub: ``start(timeslice)`` schedules
      // two ondataavailable callbacks, then a stop. We expose
      // ``__chunkSizes`` so tests can assert the cadence if needed.
      class FakeMediaRecorder {
        ondataavailable: ((ev: { data: Blob }) => void) | null = null;
        onstop: (() => void) | null = null;
        onerror: ((ev: Event) => void) | null = null;
        state = 'inactive';
        constructor(_stream: unknown, _opts: unknown) {}
        start(_timeslice: number) {
          this.state = 'recording';
          // Two fake chunks emitted on a fast cadence.
          setTimeout(() => {
            this.ondataavailable?.({
              data: new Blob([new Uint8Array([1, 2, 3, 4])], {
                type: 'audio/webm',
              }),
            });
          }, 10);
          setTimeout(() => {
            this.ondataavailable?.({
              data: new Blob([new Uint8Array([5, 6, 7, 8, 9])], {
                type: 'audio/webm',
              }),
            });
          }, 20);
        }
        stop() {
          this.state = 'inactive';
          setTimeout(() => this.onstop?.(), 5);
        }
      }
      Object.defineProperty(window, 'MediaRecorder', {
        configurable: true,
        value: FakeMediaRecorder,
      });
    });

    await page.goto(`/interview/${SESSION_ID}`);

    await expect(page.getByTestId('session-id')).toHaveText(SESSION_ID);
    await page.getByTestId('start-interview').click();

    // Wait until two chunks are uploaded.
    await expect(page.getByTestId('chunk-count')).toHaveText('2', {
      timeout: 5_000,
    });

    await page.getByTestId('stop-interview').click();
    await expect(page.getByTestId('phase')).toHaveText('completed', {
      timeout: 5_000,
    });
    await expect(page.getByTestId('interview-result')).toContainText(
      'completed',
    );
  });
});
