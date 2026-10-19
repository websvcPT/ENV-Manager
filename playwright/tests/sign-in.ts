import { expect, type Browser, type Page } from '@playwright/test';

import type { CreatedUser } from '../users-api';

/** Fill the sign-in card of the page that shows it and submit it. */
export async function submitSignIn(page: Page, name: string, password: string): Promise<void> {
  await page.locator('#user').fill(name);
  await page.locator('#pw').fill(password);
  await page.locator('#login-card button[type="submit"]').click();
}

/**
 * Sign in through the login form in a browser context of its own (the layout of the project, no
 * stored session) and return its page, so a spec can act as a scratch user next to the stored
 * superadmin session. `password` replaces the user's own and `code` answers the second factor
 * step that follows.
 */
export async function signedInAs(
  browser: Browser,
  user: CreatedUser,
  options: { password?: string; code?: string } = {},
): Promise<Page> {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const page = await context.newPage();
  await page.goto('/login');
  await submitSignIn(page, user.name, options.password ?? user.password);
  if (options.code !== undefined) {
    await page.locator('#code').fill(options.code);
    await page.locator('#login-card button[type="submit"]').click();
  }
  await expect(page).toHaveURL(/\/projects$/);
  return page;
}
