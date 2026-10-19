import type { Browser } from '@playwright/test';

import { test as base } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment, seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';
import { createHostFolder, type HostFolder } from '../host-files';
import { createService, serviceId } from '../services-api';
import { actionControls, rowAction } from './row-actions';
import { expect, test as scratchTest } from './scratch-project';

/*
 * Environment delete. Every spec makes its own scratch items below its scratch project (removed
 * with it) and never deletes seeded files.
 */

const test = scratchTest.extend<{ host: HostFolder }>({
  host: async ({}, use) => {
    const host = createHostFolder();
    try {
      await use(host);
    } finally {
      host.remove();
    }
  },
});

test.describe('delete an environment', () => {
  test('asks for the name, then deletes the file from the table and stays on the service', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'del', host.root);
    const service = await createService(request, deployment, 'app');
    const doomed = await createEnvironment(request, service, '.env.doomed', 'A=1\n');
    await createEnvironment(request, service, '.env.kept', 'B=1\n');
    await page.goto(`/services/${service}`);

    const row = page.locator('#environments tr[data-fragment="env-row"]', {
      hasText: '.env.doomed',
    });
    const remove = await rowAction(row, 'delete');
    await remove.click();
    const dialog = page.locator('#environment-delete');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Delete file .env.doomed');
    await expect(dialog).toContainText('This cannot be undone.');

    await dialog.locator('#den').fill('.env.kept');
    await dialog.locator('button[type="submit"]').click();
    await expect(dialog.locator('#den-e')).toHaveText('Type the file name exactly to confirm.');
    expect((await request.get(`/environments/${doomed}`)).status()).toBe(200);

    await dialog.locator('#den').fill('.env.doomed');
    await dialog.locator('button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/services/${service}\\?deleted=environment$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText('File deleted');
    await expect(
      page.locator('#environments tr[data-fragment="env-row"]', { hasText: '.env.doomed' }),
    ).toHaveCount(0);
    await expect(
      page.locator('#environments tr[data-fragment="env-row"]', { hasText: '.env.kept' }),
    ).toHaveCount(1);
    expect((await request.get(`/environments/${doomed}`)).status()).toBe(404);
  });

  test('deletes from the environment page and can be cancelled first', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'del', host.root);
    const service = await createService(request, deployment, 'app');
    const environment = await createEnvironment(request, service, '.env.gone', 'A=1\n');
    await page.goto(`/environments/${environment}`);

    await page.locator('#environment-delete-open').click();
    const dialog = page.locator('#environment-delete');
    await expect(dialog.locator('#den')).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();
    expect((await request.get(`/environments/${environment}`)).status()).toBe(200);

    await page.locator('#environment-delete-open').click();
    await page.locator('#den').fill('.env.gone');
    await page.locator('#delete-environment-form button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/services/${service}\\?deleted=environment$`));
    expect((await request.get(`/environments/${environment}`)).status()).toBe(404);
  });
});

async function seededFile(browser: Browser): Promise<number> {
  return seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
}

for (const user of ['admin', 'viewer'] as const) {
  base.describe(`as ${user}`, () => {
    base.use({ storageState: stateFile(user) });

    base('sees no delete control and is refused the routes', async ({ page, browser }) => {
      const environment = await seededFile(browser);
      const service = await serviceId(browser, 'Fancy Proj. Name', 'prod', 'mysql');
      await page.goto(`/environments/${environment}`);
      await expect(page.locator('#environment-delete-open')).toHaveCount(0);
      await page.goto(`/services/${service}`);
      await expect(actionControls(page.locator('#environments'), 'delete')).toHaveCount(0);
      expect((await page.request.get(`/environments/${environment}/delete`)).status()).toBe(403);
    });
  });
}
