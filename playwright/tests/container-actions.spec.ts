import type { Browser, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { stateFile } from '../global-setup';
import { createHostFolder } from '../host-files';
import { horizontalOverflow } from '../viewport';
import { actionControls } from './row-actions';
import { expect, test } from './scratch-project';

/*
 * The container actions on the stack the default run uses, which has no Docker socket: the
 * Overview offers no control, nothing breaks, and the confirmation says why an action cannot run.
 * The actions themselves run in docker-tests/container-actions-docker.spec.ts.
 */

const HTMX = { 'HX-Request': 'true' };
const CONTROLS = ['start', 'stop', 'restart', 'recreate'];

function prod(browser: Browser): Promise<number> {
  return deploymentId(browser, 'Fancy Proj. Name', 'prod');
}

function dialogAddress(id: number, service = 'mysql', action = 'restart'): string {
  return `/deployments/${id}/containers/${service}/${action}`;
}

async function expectNoControls(page: Page): Promise<void> {
  for (const key of CONTROLS) {
    await expect(actionControls(page.locator('#runtime'), key)).toHaveCount(0);
  }
}

test.describe('superadmin without a usable Docker', () => {
  test('sees no container control on the Overview and nothing breaks', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('[data-fragment="service-summary-row"]')).toHaveCount(2);
    await expectNoControls(page);
    await expect(page.locator('#container-action')).toBeHidden();
    await expect(page.locator('#container-toast')).toBeEmpty();
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('is told why when a deployment has no Compose project', async ({ page, browser }) => {
    const id = await prod(browser);
    const dialog = await page.request.get(dialogAddress(id), { headers: HTMX });
    expect(dialog.status()).toBe(200);
    const text = await dialog.text();
    expect(text).toContain('no Docker Compose project name');
    expect(text).toContain('data-container-refusal');
    expect(text).not.toContain('<form');
  });

  test('is told why when Docker is not usable', async ({ page, request, scratch }) => {
    const host = createHostFolder();
    try {
      const id = await createDeployment(request, scratch.id, 'actions-dep', host.root, 'block', 'e2e-actions');
      await page.goto(`/deployments/${id}`);
      await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
      await expectNoControls(page);
      for (const action of ['start', 'stop', 'restart', 'recreate']) {
        const dialog = await page.request.get(dialogAddress(id, 'api', action), { headers: HTMX });
        expect(dialog.status()).toBe(200);
        const text = await dialog.text();
        expect(text).toContain('The Docker socket is not mounted.');
        expect(text).not.toContain('<form');
      }
    } finally {
      host.remove();
    }
  });

  test('gets a 404 for an action or a service name that does not exist', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    for (const address of [
      dialogAddress(id, 'mysql', 'kill'),
      dialogAddress(id, '-rf'),
      dialogAddress(id, 'a%20b'),
      dialogAddress(id, 'mysql%0Aweb'),
    ]) {
      const response = await page.request.get(address, { headers: HTMX });
      expect(response.status(), address).toBe(404);
    }
  });
});

test.describe('admin with a project grant', () => {
  test.use({ storageState: stateFile('admin') });

  test('has the dialog of the actions on the Overview and no control without Docker', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#container-action')).toBeHidden();
    await expectNoControls(page);
    const dialog = await page.request.get(dialogAddress(id), { headers: HTMX });
    expect(dialog.status()).toBe(200);
  });
});

test.describe('admin with a deployment grant', () => {
  test.use({ storageState: stateFile('deploymentAdmin') });

  test('may open the confirmation', async ({ page, browser }) => {
    const id = await prod(browser);
    const dialog = await page.request.get(dialogAddress(id), { headers: HTMX });
    expect(dialog.status()).toBe(200);
  });
});

test.describe('admin with a grant on one file', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('is refused the confirmation and has no dialog on the page', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#container-action')).toHaveCount(0);
    await expectNoControls(page);
    const dialog = await page.request.get(dialogAddress(id), { headers: HTMX });
    expect(dialog.status()).toBe(403);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('has no control, no dialog and is refused the confirmation', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#container-action')).toHaveCount(0);
    await expect(page.locator('#container-toast')).toHaveCount(0);
    await expectNoControls(page);
    const dialog = await page.request.get(dialogAddress(id), { headers: HTMX });
    expect(dialog.status()).toBe(403);
  });
});

test.describe('admin without grants', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('does not find the deployment', async ({ page, browser }) => {
    const id = await prod(browser);
    const dialog = await page.request.get(dialogAddress(id), { headers: HTMX });
    expect(dialog.status()).toBe(404);
  });
});
