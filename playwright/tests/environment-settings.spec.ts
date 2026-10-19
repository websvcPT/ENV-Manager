import type { Browser } from '@playwright/test';

import { test as base } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment, seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';
import { createHostFolder, type HostFolder } from '../host-files';
import { createService, serviceId } from '../services-api';
import { horizontalOverflow } from '../viewport';
import { actionControls, rowAction } from './row-actions';
import { expect, test as scratchTest } from './scratch-project';

/*
 * Environment settings: paths, file attributes and the policy overrides. Every spec makes its
 * own scratch items below its scratch project (removed with it) and never edits seeded files.
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

test.describe('environment settings', () => {
  test('is offered from the header and the row actions and shows the stored values', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'cfg', host.root);
    const service = await createService(request, deployment, 'app');
    const environment = await createEnvironment(request, service, '.env.app', 'A=1\n');

    await page.goto(`/services/${service}`);
    const row = page.locator('#environments tr[data-fragment="env-row"]', { hasText: '.env.app' });
    const settings = await rowAction(row, 'settings');
    await expect(settings).toHaveAttribute('href', `/environments/${environment}/settings`);

    await page.goto(`/environments/${environment}`);
    await page.locator('#environment-settings-open').click();
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}/settings$`));
    await expect(page.locator('#tp')).toHaveValue('environments/.env.app');
    await expect(page.locator('#tp-h')).toContainText(`${host.root}/environments/.env.app`);
    for (const name of ['dist_missing_policy', 'drift_policy', 'missing_target_policy']) {
      await expect(page.locator(`input[name="${name}"][value=""]`)).toBeChecked();
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('saves the overrides and the file attributes and shows them again', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'cfg', host.root);
    const service = await createService(request, deployment, 'app');
    const environment = await createEnvironment(request, service, '.env.app', 'A=1\n');
    await page.goto(`/environments/${environment}/settings`);

    await page.locator('#m').fill('640');
    await page.locator('#dp').fill('environments/.env.app-reference');
    await page.locator('input[name="drift_policy"][value="overwrite"]').check();
    await page.locator('input[name="dist_missing_policy"][value="warn"]').check();
    await page.locator('input[name="missing_target_policy"][value="halt"]').check();
    await page.locator('#env-settings button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}\\?updated=true$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Settings saved');

    await page.goto(`/environments/${environment}/settings`);
    await expect(page.locator('#m')).toHaveValue('0640');
    await expect(page.locator('#dp')).toHaveValue('environments/.env.app-reference');
    await expect(page.locator('input[name="drift_policy"][value="overwrite"]')).toBeChecked();
    await expect(page.locator('input[name="dist_missing_policy"][value="warn"]')).toBeChecked();
    await expect(page.locator('input[name="missing_target_policy"][value="halt"]')).toBeChecked();

    await page.locator('input[name="drift_policy"][value=""]').check();
    await page.locator('#env-settings button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}\\?updated=true$`));
    await page.goto(`/environments/${environment}/settings`);
    await expect(page.locator('input[name="drift_policy"][value=""]')).toBeChecked();
  });

  test('refuses a bad mode, a dist file equal to the target and a taken target path', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'cfg', host.root);
    const service = await createService(request, deployment, 'app');
    const environment = await createEnvironment(request, service, '.env.app', 'A=1\n');
    await createEnvironment(request, service, '.env.other', 'B=1\n');
    await page.goto(`/environments/${environment}/settings`);

    await page.locator('#m').fill('9');
    await page.locator('#dp').fill('environments/.env.app');
    await page.locator('#env-settings button[type="submit"]').click();
    await expect(page.locator('#m-e')).toContainText('octal mode');
    await expect(page.locator('#dp-e')).toContainText('different file from the target');

    await page.locator('#dp').fill('');
    await page.locator('#tp').fill('environments/.env.other');
    await page.locator('#env-settings button[type="submit"]').click();
    await expect(page.locator('#tp-e')).toContainText('already uses this target path');
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}/settings$`));
  });

  test('says nothing is left behind when a never deployed file changes its target path', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'cfg', host.root);
    const service = await createService(request, deployment, 'app');
    const environment = await createEnvironment(request, service, '.env.app', 'A=1\n');
    await page.goto(`/environments/${environment}/settings`);

    await page.locator('#tp').fill('environments/.env.moved');
    const notice = page.locator('[data-fragment="target-move"]');
    await expect(notice).toContainText('never deployed', { timeout: 10_000 });
    await expect(page.locator('#ack-move')).toHaveCount(0);
    await page.locator('#env-settings button[type="submit"]').click();
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}\\?updated=true$`));
    await expect(page.locator('[data-header-details]')).toContainText('never deployed');
    await page.goto(`/services/${service}`);
    await expect(
      page.locator('#environments tr[data-fragment="env-row"]', { hasText: '.env.app' }),
    ).toContainText('environments/.env.moved');
  });

  test('refuses a target path outside the deployment root with the reason live', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'cfg', host.root);
    const service = await createService(request, deployment, 'app');
    const environment = await createEnvironment(request, service, '.env.app', 'A=1\n');
    await page.goto(`/environments/${environment}/settings`);
    await page.locator('#tp').fill('../escape');
    await expect(page.locator('#tp-e')).toContainText('outside', { timeout: 10_000 });
  });
});

async function seededFile(browser: Browser): Promise<number> {
  return seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
}

for (const user of ['admin', 'viewer'] as const) {
  base.describe(`as ${user}`, () => {
    base.use({ storageState: stateFile(user) });

    base('sees no settings control and is refused the routes', async ({ page, browser }) => {
      const environment = await seededFile(browser);
      const service = await serviceId(browser, 'Fancy Proj. Name', 'prod', 'mysql');
      await page.goto(`/environments/${environment}`);
      await expect(page.locator('#environment-settings-open')).toHaveCount(0);
      await page.goto(`/services/${service}`);
      await expect(actionControls(page.locator('#environments'), 'settings')).toHaveCount(0);
      for (const path of [
        `/environments/${environment}/settings`,
        `/environments/${environment}/settings/target-check`,
      ]) {
        expect((await page.request.get(path)).status(), path).toBe(403);
      }
    });
  });
}
