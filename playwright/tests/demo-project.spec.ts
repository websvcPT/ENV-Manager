import type { Browser, Page } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { stateFile } from '../global-setup';
import { serviceId } from '../services-api';
import { horizontalOverflow, isWide } from '../viewport';

function demoService(browser: Browser, name: string): Promise<number> {
  return serviceId(browser, 'demo', 'stack', name);
}

/** Environment rows of whichever layout is visible: the table from md up, the list below. */
function fileRows(page: Page) {
  const selector = isWide(page) ? 'tr[data-env]' : 'li[data-fragment="env-row"]';
  return page.locator('#environments').locator(selector);
}

function fileNames(page: Page) {
  return page.locator('#environments').getByRole('link', { name: /^(\.env\.|demo-ca\.pem)/ });
}

test.describe('superadmin', () => {
  test('lists the demo project with its one deployment', async ({ page }) => {
    await page.goto('/projects');
    const row = page.locator('[data-fragment="project-row"]', { hasText: 'demo' });
    await expect(row.locator('th[scope="row"] a')).toHaveAttribute('href', /^\/projects\/\d+$/);
    await expect(row.locator('td').first()).toHaveText('1');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('shows the stack deployment with its api and db services', async ({ page, browser }) => {
    const id = await deploymentId(browser, 'demo', 'stack');
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('stack');
    await expect(page.locator('[data-fragment="page-header"] p')).toContainText('Project demo');
    const services = page.locator('#services [data-fragment="service-row"]');
    await expect(services.locator('th a')).toHaveText(['api', 'db']);
    await expect(services.filter({ hasText: 'api' }).locator('td').first()).toHaveText('2');
    await expect(services.filter({ hasText: 'db' }).locator('td').first()).toHaveText('2');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('shows the files and the stored command of each service', async ({ page, browser }) => {
    await page.goto(`/services/${await demoService(browser, 'api')}`);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('api');
    await expect(fileRows(page)).toHaveCount(2);
    await expect(fileNames(page)).toHaveText(['.env.api', 'demo-ca.pem']);
    await expect(page.locator('#postdeploy')).toContainText(
      'docker compose up -d --no-deps --force-recreate api',
    );

    await page.goto(`/services/${await demoService(browser, 'db')}`);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('db');
    await expect(fileRows(page)).toHaveCount(2);
    await expect(fileNames(page)).toHaveText(['.env.db', '.env.db.secrets']);
    await expect(page.locator('#postdeploy')).toContainText(
      'docker compose up -d --no-deps --force-recreate db',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('environment grant', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('sees the project limited to .env.db and only that file of the db service', async ({
    page,
    browser,
  }) => {
    await page.goto('/projects');
    await expect(
      page.locator('[data-fragment="project-row"]', { hasText: 'demo' }),
    ).toContainText('Access limited to .env.db');

    await page.goto(`/services/${await demoService(browser, 'db')}`);
    await expect(fileRows(page)).toHaveCount(1);
    await expect(fileNames(page)).toHaveText(['.env.db']);
    await expect(page.locator('main')).not.toContainText('.env.db.secrets');
  });

  test('gets no access to the api service', async ({ page, browser }) => {
    const response = await page.request.get(`/services/${await demoService(browser, 'api')}`);
    expect(response.status()).toBe(404);
  });
});
