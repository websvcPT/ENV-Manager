import type { Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createService } from '../services-api';
import { expect, test } from '../tests/scratch-project';
import { horizontalOverflow, TOUCH_TARGET, usesMenus } from '../viewport';

/*
 * The Post-deploy tab of the Settings against a usable Docker: the live status, the enabled
 * switch, a stored change that survives a reload, and what the service page says when the
 * switch is off. Run by `make e2e-demo`. The switch is always put back on.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;

function form(page: Page) {
  return page.locator('#post-deploy-switch');
}

function toast(page: Page) {
  return form(page).locator('[data-fragment="toast"]');
}

async function switchOn(page: Page): Promise<void> {
  await page.goto('/settings/post-deploy');
  await form(page).locator('#pd-enabled').check();
  await form(page).locator('button[type="submit"]').click();
  await expect(toast(page)).toBeVisible();
}

test.describe('post-deploy tab with a usable Docker', () => {
  test('shows the live status and an enabled switch', async ({ page }) => {
    await page.goto('/settings/post-deploy');
    const panel = page.locator('[data-fragment="settings-postdeploy"]');
    await expect(panel.locator('[role="note"]')).toHaveCount(0);
    await expect(panel.locator('#docker-socket [data-status="deployed"]')).toHaveText('Mounted');
    await expect(panel.locator('#docker-daemon [data-status="deployed"]')).toHaveText('Reachable');
    await expect(panel.locator('#docker-compose')).toContainText(/v\d+\.\d+/);
    await expect(form(page).locator('#pd-enabled')).toBeEnabled();
    await expect(form(page).locator('#pd-enabled')).toBeChecked();
    await expect(form(page)).toContainText('Services with a command run it once after a deploy.');
    const save = form(page).locator('button[type="submit"]');
    await expect(save).toBeEnabled();
    if (usesMenus(page)) {
      const box = await save.boundingBox();
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('turns the switch off, keeps it after a reload and turns it on again', async ({
    page,
    request,
    scratch,
  }) => {
    expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
    const deployment = await createDeployment(request, scratch.id, 'stack', STACK);
    const service = await createService(request, deployment, 'api');
    try {
      await page.goto('/settings/post-deploy');
      await form(page).locator('#pd-enabled').uncheck();
      await form(page).locator('button[type="submit"]').click();
      await expect(toast(page)).toContainText('Post-deploy settings saved');
      await expect(toast(page)).toContainText('Deploys skip the stored commands and say why.');
      await expect(form(page).locator('#pd-enabled')).not.toBeChecked();

      await page.reload();
      await expect(form(page).locator('#pd-enabled')).not.toBeChecked();
      await form(page).locator('button[type="submit"]').click();
      await expect(toast(page)).toContainText('Nothing to save');

      await page.goto(`/services/${service}`);
      const status = page.locator('#postdeploy [data-fragment="docker-status"]');
      await expect(status).toContainText('Docker socket mounted');
      await expect(status.locator('[data-status="skipped"]')).toHaveText('Switched off in settings');
      await status.getByRole('link', { name: 'Open post-deploy settings' }).click();
      await expect(page).toHaveURL(/\/settings\/post-deploy$/);

      await form(page).locator('#pd-enabled').check();
      await form(page).locator('button[type="submit"]').click();
      await expect(toast(page)).toContainText('Services with a command run it after a deploy.');
      await page.goto(`/services/${service}`);
      await expect(
        page.locator('#postdeploy [data-fragment="docker-status"] [data-status="skipped"]'),
      ).toHaveCount(0);
    } finally {
      await switchOn(page);
    }
  });
});
