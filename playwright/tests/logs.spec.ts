import type { Browser, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { stateFile } from '../global-setup';
import { createHostFolder } from '../host-files';
import { horizontalOverflow } from '../viewport';
import { expect, test } from './scratch-project';

/*
 * The Logs tab on the stack the default run uses, which has no Docker socket: who sees the tab and
 * who may open it, and what the tab, the lines request and the download say without a usable
 * Docker or a Compose project. The lines themselves, the masking and the controls run in
 * docker-tests/logs-docker.spec.ts.
 */

const HTMX = { 'HX-Request': 'true' };

function prod(browser: Browser): Promise<number> {
  return deploymentId(browser, 'Fancy Proj. Name', 'prod');
}

function tabs(page: Page) {
  return page.locator('nav[aria-label="Sections"] a');
}

test.describe('superadmin without a usable Docker', () => {
  test('sees the tab and is told a deployment has no Compose project', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(tabs(page).filter({ hasText: 'Logs' })).toHaveAttribute(
      'href',
      `/deployments/${id}/logs`,
    );
    await tabs(page).filter({ hasText: 'Logs' }).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${id}/logs$`));
    await expect(tabs(page).filter({ hasText: 'Logs' })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('[data-logs-state="no_project"]')).toContainText(
      'no Docker Compose project name',
    );
    await expect(page.getByRole('link', { name: 'Set the project name in Settings' })).toBeVisible();
    await expect(page.locator('#log-poller')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('is told Docker is not usable and the lines request says so without breaking', async ({
    page,
    request,
    scratch,
  }) => {
    const host = createHostFolder();
    try {
      const id = await createDeployment(request, scratch.id, 'logs-dep', host.root, 'block', 'e2e-logs');
      await page.goto(`/deployments/${id}/logs`);
      await expect(page.locator('[data-logs-state="unavailable"]')).toContainText(
        'The logs could not be read: The Docker socket is not mounted.',
      );
      await expect(page.locator('#log-poller')).toHaveCount(0);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

      const lines = await page.request.get(`/deployments/${id}/logs/lines`, { headers: HTMX });
      expect(lines.status()).toBe(200);
      const text = await lines.text();
      expect(text).toContain('The logs could not be read: The Docker socket is not mounted.');
      expect(text).toContain('data-log-notice');
      expect(text).toContain('hx-swap-oob="innerHTML:#log-lines"');

      const download = await page.request.get(`/deployments/${id}/logs/download`);
      expect(download.status()).toBe(409);
      expect(download.headers()['cache-control']).toBe('no-store');
      expect(download.headers()['content-disposition']).toBeUndefined();
    } finally {
      host.remove();
    }
  });

  test('gets a 404 for a service name that cannot be one and a 400 for a bad cursor', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    for (const query of ['service=-rf', 'service=a%20b', 'service=mysql%0Aweb']) {
      const response = await page.request.get(`/deployments/${id}/logs/lines?${query}`, {
        headers: HTMX,
      });
      expect(response.status(), query).toBe(404);
    }
    const bad = await page.request.get(`/deployments/${id}/logs/lines?cursor=yesterday`, {
      headers: HTMX,
    });
    expect(bad.status()).toBe(400);
  });
});

test.describe('admin with a project grant', () => {
  test.use({ storageState: stateFile('admin') });

  test('sees the tab and may open it', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/files`);
    await expect(tabs(page).filter({ hasText: 'Logs' })).toBeVisible();
    await page.goto(`/deployments/${id}/logs`);
    await expect(page.locator('[data-logs-state="no_project"]')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Set the project name in Settings' })).toHaveCount(0);
  });
});

test.describe('admin with a deployment grant', () => {
  test.use({ storageState: stateFile('deploymentAdmin') });

  test('sees the tab and may open the lines request', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(tabs(page).filter({ hasText: 'Logs' })).toBeVisible();
    const lines = await page.request.get(`/deployments/${id}/logs/lines`, { headers: HTMX });
    expect(lines.status()).toBe(200);
  });
});

test.describe('admin with a grant on one file', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('has no tab and is refused the page, the lines and the download', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(tabs(page).filter({ hasText: 'Overview' })).toBeVisible();
    await expect(tabs(page).filter({ hasText: 'Logs' })).toHaveCount(0);
    expect((await page.request.get(`/deployments/${id}/logs`)).status()).toBe(403);
    expect((await page.request.get(`/deployments/${id}/logs/lines`, { headers: HTMX })).status()).toBe(403);
    expect((await page.request.get(`/deployments/${id}/logs/download`)).status()).toBe(403);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('has no tab and is refused the page, the lines and the download', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(tabs(page).filter({ hasText: 'Overview' })).toBeVisible();
    await expect(tabs(page).filter({ hasText: 'Logs' })).toHaveCount(0);
    await expect(page.locator('[data-row-action="logs"]')).toHaveCount(0);
    expect((await page.request.get(`/deployments/${id}/logs`)).status()).toBe(403);
    expect((await page.request.get(`/deployments/${id}/logs/lines`, { headers: HTMX })).status()).toBe(403);
    expect((await page.request.get(`/deployments/${id}/logs/download`)).status()).toBe(403);
  });
});

test.describe('admin without grants', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('does not find the deployment', async ({ page, browser }) => {
    const id = await prod(browser);
    expect((await page.request.get(`/deployments/${id}/logs`)).status()).toBe(404);
    expect((await page.request.get(`/deployments/${id}/logs/lines`, { headers: HTMX })).status()).toBe(404);
  });
});
