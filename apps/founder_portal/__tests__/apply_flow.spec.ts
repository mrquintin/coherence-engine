import { expect, test, type Route } from '@playwright/test';

/**
 * Happy-path apply flow:
 *   1. Walk the multi-step form (company → product → market → ask).
 *   2. Submit; backend mocked to return application_id.
 *   3. Land on upload step; mock initiate + complete; verify deck upload.
 *   4. Navigate to status page; mock status polling; verify timeline.
 */

const APPLICATION_ID = 'app_test123';

async function mockBackendAuthRoute(page: import('@playwright/test').Page) {
  await page.route('**/api/auth*', async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const method = req.method();

    // Polling endpoint (GET)
    if (method === 'GET' && url.searchParams.get('action') === 'application_status') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          application_id: APPLICATION_ID,
          status: 'scoring_in_progress',
          one_liner: 'Test',
          preferred_channel: 'web_voice',
          scoring_mode: 'enforce',
          created_at: new Date().toISOString(),
        }),
      });
      return;
    }
    if (method === 'GET' && url.searchParams.get('action') === 'artifact_url') {
      await route.fulfill({ status: 404, body: '' });
      return;
    }

    // POST actions: parse body
    if (method === 'POST') {
      const body = req.postDataJSON() as { action?: string };
      if (body.action === 'create_application') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ application_id: APPLICATION_ID }),
        });
        return;
      }
      if (body.action === 'upload_initiate') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            upload_id: 'upl_abc',
            upload_url: 'https://signed.example/put?expires=600&signed_url=1',
            headers: { 'Content-Type': 'application/pdf' },
            expires_at: new Date(Date.now() + 600_000).toISOString(),
            key: `applications/${APPLICATION_ID}/deck/abc-deck.pdf`,
            uri: `coh://local/default/applications/${APPLICATION_ID}/deck/abc-deck.pdf`,
            max_bytes: 25 * 1024 * 1024,
          }),
        });
        return;
      }
      if (body.action === 'upload_complete') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            upload_id: 'upl_abc',
            uri: `coh://local/default/applications/${APPLICATION_ID}/deck/abc-deck.pdf`,
            size_bytes: 1024,
            status: 'completed',
          }),
        });
        return;
      }
    }
    await route.fulfill({ status: 404, body: '' });
  });

  // Direct PUT to the signed URL — short-circuit to a 200.
  await page.route('https://signed.example/**', async (route: Route) => {
    await route.fulfill({ status: 200, body: '' });
  });
}

test.describe('apply flow', () => {
  test('happy path: walks all steps, submits, uploads deck', async ({ page }) => {
    await mockBackendAuthRoute(page);

    await page.goto('/apply');

    // Step 1: company
    await expect(page.getByRole('heading', { name: /Apply to the Coherence Fund/i })).toBeVisible();
    await page.getByTestId('full_name').fill('Jane Founder');
    await page.getByTestId('email').fill('jane@example.com');
    await page.getByTestId('company_name').fill('Acme Labs');
    await page.getByTestId('country').fill('US');
    await page.getByTestId('next-step').click();

    // Step 2: product
    await page.getByTestId('one_liner').fill('We automate finance ops for SMBs');
    await page.getByTestId('next-step').click();

    // Step 3: market
    await page
      .getByTestId('market_summary')
      .fill('Mid-market SMB finance teams; $40B addressable.');
    await page.getByTestId('next-step').click();

    // Step 4: ask → submit
    await page.getByTestId('requested_check_usd').fill('250000');
    await page
      .getByTestId('use_of_funds_summary')
      .fill('Hire two engineers, run six pilots, 12-month runway.');
    await page.getByTestId('submit-application').click();

    // Step 5: upload — uses application id returned by mock
    await expect(page.getByText(/Application created\./i)).toBeVisible();

    // Set up a fake file and dispatch into the deck input.
    const fileInput = page.locator('input[type="file"]').first();
    await fileInput.setInputFiles({
      name: 'deck.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('%PDF-1.4 stub bytes'),
    });
    await expect(
      page.getByText(/Uploaded \(/i),
    ).toBeVisible({ timeout: 5000 });

    // Navigate to status page
    await page.goto(`/applications/${APPLICATION_ID}`);
    await expect(page.getByTestId('application-id')).toHaveText(APPLICATION_ID);
    await expect(page.getByTestId('application-status')).toContainText(
      /scoring/i,
      { timeout: 10_000 },
    );
  });
});
