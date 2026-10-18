import { expect, test as base, type Browser, type Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { createProject, deleteProjectIfPresent } from '../projects-api';
import { createService } from '../services-api';
import { createUser, disableUser, uniqueUserName, type CreatedUser } from '../users-api';

export const WIDE_BREAKPOINT = 768;
export const TOUCH_TARGET = 44;
const SAMPLE_A = '/srv/deployments/sample-a';

export interface Scratch {
  projectId: number;
  projectName: string;
  depA: number;
  depB: number;
  serviceA: number;
  serviceB: number;
  envA1: number;
  envA2: number;
  envB: number;
}

/**
 * The base test plus a scratch project with two deployments (the first with one service of two
 * environments, the second with one service of one) and a `makeUser` that creates users through
 * the form post and disables them again after the test, because users cannot be deleted. Grants
 * only ever go to scratch users on the scratch project, never to seeded users.
 */
export const test = base.extend<{
  scratch: Scratch;
  makeUser: (group?: 'admin' | 'viewer', grants?: string[]) => Promise<CreatedUser>;
}>({
  scratch: async ({ request }, use, testInfo) => {
    const suffix = Math.random().toString(36).slice(2, 7);
    const projectName = `e2e-detail-${Date.now()}-${suffix}-${testInfo.project.name}`;
    const projectId = await createProject(request, projectName);
    try {
      const depA = await createDeployment(request, projectId, 'dep-a', SAMPLE_A);
      const depB = await createDeployment(request, projectId, 'dep-b', SAMPLE_A);
      const serviceA = await createService(request, depA, 'cache');
      const serviceB = await createService(request, depB, 'queue');
      const envA1 = await createEnvironment(request, serviceA, '.env.cache', 'A=1\n');
      const envA2 = await createEnvironment(request, serviceA, '.env.cache2', 'B=1\n');
      const envB = await createEnvironment(request, serviceB, '.env.queue', 'C=1\n');
      await use({ projectId, projectName, depA, depB, serviceA, serviceB, envA1, envA2, envB });
    } finally {
      await deleteProjectIfPresent(request, projectId);
    }
  },
  makeUser: async ({ request }, use, testInfo) => {
    const created: CreatedUser[] = [];
    await use(async (group = 'viewer', grants = []) => {
      const user = await createUser(request, uniqueUserName(testInfo.project.name), group, grants);
      created.push(user);
      return user;
    });
    for (const user of created) {
      await disableUser(request, user.id);
    }
  },
});

export { expect };

export function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

export async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/** Sign in through the login form in a browser of its own and return the page. */
export async function signedInAs(browser: Browser, user: CreatedUser): Promise<Page> {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const page = await context.newPage();
  await page.goto('/login');
  await page.locator('#user').fill(user.name);
  await page.locator('#pw').fill(user.password);
  await page.locator('#login-card button[type="submit"]').click();
  await expect(page).toHaveURL(/\/projects$/);
  return page;
}
