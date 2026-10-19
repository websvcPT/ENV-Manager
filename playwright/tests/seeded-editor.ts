import { expect, type Browser, type Page } from '@playwright/test';

import { seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';

/** Values of the seeded editor file that no masked page may carry. */
export const SEEDED_SECRETS = [
  'Zq7vK2mPw9',
  'Tn4xB8cL1d',
  'Ke3rH6vNa5',
  'MIIDdzCCAl7sentinelCertBody',
];

/** The seeded file with comments, secrets and a certificate that the masking specs read. */
export function seededEditor(browser: Browser): Promise<number> {
  return seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
}

/** Fail when the page, markup included, carries any of the values. */
export async function expectNoValueInPage(page: Page, ...values: string[]): Promise<void> {
  const html = await page.content();
  for (const value of values) {
    expect(html).not.toContain(value);
  }
}

/**
 * Run `check` on a page of each role that has no grant on the seeded editor file (an admin with
 * no grants and a viewer scoped to other files), each in a browser context of its own.
 */
export async function asRolesWithoutAccess(
  browser: Browser,
  check: (page: Page) => Promise<void>,
): Promise<void> {
  for (const user of ['ungrantedAdmin', 'envScopedViewer'] as const) {
    const context = await browser.newContext({ storageState: stateFile(user) });
    try {
      await check(await context.newPage());
    } finally {
      await context.close();
    }
  }
}
