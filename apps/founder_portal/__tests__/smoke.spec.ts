import { expect, test } from '@playwright/test';

test('landing page renders and sign-in redirects to Supabase auth UI', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Founder Portal' })).toBeVisible();

  const signinButton = page.getByTestId('signin-button');
  await expect(signinButton).toBeVisible();

  await Promise.all([
    page.waitForURL((url) => url.pathname.includes('/auth/v1/authorize')),
    signinButton.click(),
  ]);

  const url = new URL(page.url());
  expect(url.pathname).toContain('/auth/v1/authorize');
  expect(url.searchParams.get('provider')).toBe('email');
});
