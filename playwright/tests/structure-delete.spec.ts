import type { Browser, Page } from '@playwright/test';

import { test as base } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { stateFile } from '../global-setup';
import { createHostFolder, type HostFolder } from '../host-files';
import { createService, serviceId } from '../services-api';
import { horizontalOverflow } from '../viewport';
import { actionControls, rowAction } from './row-actions';
import { expect, test as scratchTest } from './scratch-project';

/*
 * Service rename and delete and deployment delete. Every spec makes its own scratch items below
 * its scratch project (removed with it) and never touches seeded structure.
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

function serviceRow(page: Page, name: string) {
  return page.locator('#services [data-fragment="service-row"]', { hasText: name });
}

function deploymentRow(page: Page, name: string) {
  return page.locator('#deployments-table [data-fragment="deployment-row"]', { hasText: name });
}

test.describe('rename a service', () => {
  test('saves a new name from the service page and shows the toast', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'rn', host.root);
    const service = await createService(request, deployment, 'before');
    await page.goto(`/services/${service}`);
    await page.locator('#service-rename-open').click();
    await expect(page).toHaveURL(new RegExp(`/services/${service}/rename$`));
    await expect(page.locator('#svc-name')).toHaveValue('before');

    await page.locator('#svc-name').fill('after');
    await page.locator('#service-rename button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/services/${service}\\?renamed=true$`));
    await expect(page.locator('[data-item-name]')).toHaveText('after');
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Service renamed');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('refuses a name that is not a slug or is taken and keeps what was typed', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'rn', host.root);
    const service = await createService(request, deployment, 'first');
    await createService(request, deployment, 'second');
    await page.goto(`/services/${service}/rename`);

    await page.locator('#svc-name').fill('Not A Slug');
    await page.locator('#service-rename button[type="submit"]').click();
    await expect(page.locator('#svc-name-e')).toContainText('lowercase letters');
    await expect(page.locator('#svc-name')).toHaveValue('Not A Slug');

    await page.locator('#svc-name').fill('second');
    await page.locator('#service-rename button[type="submit"]').click();
    await expect(page.locator('#svc-name-e')).toHaveText(
      'A service named second already exists in this deployment.',
    );
    await expect(page).toHaveURL(new RegExp(`/services/${service}/rename$`));
  });

  test('is offered from the services table row actions', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'rn', host.root);
    const service = await createService(request, deployment, 'listed');
    await page.goto(`/deployments/${deployment}`);
    const rename = await rowAction(serviceRow(page, 'listed'), 'rename');
    await expect(rename).toHaveAttribute('href', `/services/${service}/rename`);
  });
});

test.describe('delete a service', () => {
  test('asks for the name, then deletes the service, its files and returns to the deployment', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'del', host.root);
    const service = await createService(request, deployment, 'doomed');
    await createService(request, deployment, 'kept');
    await createEnvironment(request, service, '.env.doomed', 'A=1\n');
    await page.goto(`/deployments/${deployment}`);

    const remove = await rowAction(serviceRow(page, 'doomed'), 'delete');
    await remove.click();
    const dialog = page.locator('#service-delete');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Delete service doomed');
    await expect(dialog).toContainText('This cannot be undone.');

    await dialog.locator('#dsn').fill('wrong');
    await dialog.locator('button[type="submit"]').click();
    await expect(dialog.locator('#dsn-e')).toHaveText('Type the service name exactly to confirm.');
    await expect(dialog.locator('#dsn')).toHaveValue('wrong');
    expect((await request.get(`/services/${service}`)).status()).toBe(200);

    await dialog.locator('#dsn').fill('doomed');
    await dialog.locator('button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${deployment}\\?deleted=service$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Service deleted');
    await expect(serviceRow(page, 'doomed')).toHaveCount(0);
    await expect(serviceRow(page, 'kept')).toHaveCount(1);
    expect((await request.get(`/services/${service}`)).status()).toBe(404);
  });

  test('can be cancelled from the service page and nothing is deleted', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'del', host.root);
    const service = await createService(request, deployment, 'stays');
    await page.goto(`/services/${service}`);
    await page.locator('#service-delete-open').click();
    const dialog = page.locator('#service-delete');
    await expect(dialog.locator('#dsn')).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();
    expect((await request.get(`/services/${service}`)).status()).toBe(200);
  });

  test('deletes from the service page and lands on the deployment', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'del', host.root);
    const service = await createService(request, deployment, 'gone');
    await page.goto(`/services/${service}`);
    await page.locator('#service-delete-open').click();
    await page.locator('#dsn').fill('gone');
    await page.locator('#delete-service-form button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${deployment}\\?deleted=service$`));
    await expect(serviceRow(page, 'gone')).toHaveCount(0);
  });
});

test.describe('delete a deployment', () => {
  test('asks for the name, then deletes it with its services and returns to the project', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const doomed = await createDeployment(request, scratch.id, 'doomed', host.root);
    await createService(request, doomed, 'inside');
    const other = createHostFolder();
    try {
      const kept = await createDeployment(request, scratch.id, 'kept', other.root);
      await page.goto(`/projects/${scratch.id}`);

      const remove = await rowAction(deploymentRow(page, 'doomed'), 'delete');
      await remove.click();
      const dialog = page.locator('#deployment-delete');
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText('Delete deployment doomed');
      await expect(dialog).toContainText('its 1 service');

      await dialog.locator('#ddn').fill('kept');
      await dialog.locator('button[type="submit"]').click();
      await expect(dialog.locator('#ddn-e')).toHaveText(
        'Type the deployment name exactly to confirm.',
      );
      expect((await request.get(`/deployments/${doomed}`)).status()).toBe(200);

      await dialog.locator('#ddn').fill('doomed');
      await dialog.locator('button[type="submit"]').click();
      await expect(page).toHaveURL(new RegExp(`/projects/${scratch.id}\\?deleted=deployment$`));
      await expect(page.locator('[data-fragment="toast"]')).toContainText('Deployment deleted');
      await expect(deploymentRow(page, 'doomed')).toHaveCount(0);
      await expect(deploymentRow(page, 'kept')).toHaveCount(1);
      expect((await request.get(`/deployments/${doomed}`)).status()).toBe(404);
      expect((await request.get(`/deployments/${kept}`)).status()).toBe(200);
    } finally {
      other.remove();
    }
  });

  test('can be cancelled from the deployment page and nothing is deleted', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'stays', host.root);
    await page.goto(`/deployments/${deployment}`);
    await page.locator('#deployment-delete-open').click();
    const dialog = page.locator('#deployment-delete');
    await expect(dialog.locator('#ddn')).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();
    expect((await request.get(`/deployments/${deployment}`)).status()).toBe(200);
  });
});

async function seededPages(browser: Browser): Promise<{ deployment: number; service: number }> {
  return {
    deployment: await deploymentId(browser, 'Fancy Proj. Name', 'prod'),
    service: await serviceId(browser, 'Fancy Proj. Name', 'prod', 'web'),
  };
}

for (const user of ['admin', 'viewer'] as const) {
  base.describe(`as ${user}`, () => {
    base.use({ storageState: stateFile(user) });

    base('sees no rename or delete control and is refused the routes', async ({
      page,
      browser,
    }) => {
      const { deployment, service } = await seededPages(browser);
      await page.goto(`/deployments/${deployment}`);
      await expect(actionControls(page.locator('#services'), 'delete')).toHaveCount(0);
      await expect(actionControls(page.locator('#services'), 'rename')).toHaveCount(0);
      await expect(page.locator('#deployment-delete-open')).toHaveCount(0);
      await page.goto(`/services/${service}`);
      await expect(page.locator('#service-rename-open')).toHaveCount(0);
      await expect(page.locator('#service-delete-open')).toHaveCount(0);
      for (const path of [
        `/services/${service}/rename`,
        `/services/${service}/delete`,
        `/deployments/${deployment}/delete`,
      ]) {
        expect((await page.request.get(path)).status(), path).toBe(403);
      }
    });
  });
}
