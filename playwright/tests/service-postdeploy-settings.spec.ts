import type { Browser, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { stateFile } from '../global-setup';
import { csrfHeaders } from '../projects-api';
import { createService, serviceId } from '../services-api';
import { expect, test } from './scratch-project';

/*
 * The post-deploy command settings on the development stack, which has no Docker socket, so the
 * superadmin sees the disabled state. The states that need a usable Docker (save, refusals, the
 * compose check) are covered against the real daemon by the specs in docker-tests, which
 * `make e2e-demo` runs.
 */

const SAMPLE_A = '/srv/deployments/sample-a';
const UNAVAILABLE = 'The Docker socket is not mounted.';
const STORED_COMMAND = 'docker compose up -d --no-deps web';

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

function panel(page: Page) {
  return page.locator('#postdeploy');
}

function settings(page: Page) {
  return page.locator('#postdeploy-settings');
}

/** Make every control of the settings form usable, as a stale page or a script would. */
async function enableControls(page: Page): Promise<void> {
  await settings(page).locator('[disabled]').evaluateAll((elements) => {
    for (const element of elements) {
      element.removeAttribute('disabled');
    }
  });
}

function stagingWeb(browser: Browser): Promise<number> {
  return serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
}

function prodMysql(browser: Browser): Promise<number> {
  return serviceId(browser, 'Fancy Proj. Name', 'prod', 'mysql');
}

test.describe('superadmin without a Docker socket', () => {
  test('sees the status line, the disabled form with the stored command and the reason', async ({
    page,
    browser,
  }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);

    const status = panel(page).locator('[data-fragment="docker-status"]');
    await expect(status).toContainText('Docker socket not mounted');
    await expect(status.locator('[data-status="blocked"]')).toBeVisible();
    await expect(status.getByRole('link', { name: 'Open post-deploy settings' })).toHaveAttribute(
      'href',
      '/settings/post-deploy',
    );

    const form = settings(page);
    await expect(form.locator('h3')).toHaveText('Command settings');
    await expect(form.locator('[data-status="superadmin"]')).toBeVisible();
    await expect(form.locator('[role="note"]')).toContainText(
      'The Docker socket is not mounted, so post-deploy commands are off. These fields are disabled and saving is refused. Mount /var/run/docker.sock and recreate the container to enable the feature.',
    );
    await expect(form.locator('#wd')).toBeDisabled();
    await expect(form.locator('#wd')).toHaveValue('');
    await expect(form.locator('#wd')).toHaveAttribute('placeholder', '/opt/rentals/staging');
    await expect(form.locator('#cmds')).toBeDisabled();
    await expect(form.locator('#cmds')).toHaveValue(STORED_COMMAND);
    await expect(form.locator('#timeout')).toBeDisabled();
    await expect(form.locator('#timeout')).toHaveValue('120');
    await expect(form.locator('input[name="run_only_if_changed"]')).toBeDisabled();
    await expect(form.locator('input[name="run_only_if_changed"]')).toBeChecked();
    await expect(form.getByRole('button', { name: 'Test command' })).toBeDisabled();
    await expect(form.getByRole('button', { name: 'Save settings' })).toBeDisabled();
    await expect(form.locator('#test-result')).toHaveText(
      'docker compose config --quiet checks the path and the Compose file.',
    );
    await expect(panel(page).locator('[data-fragment="toast"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a save sent anyway is refused and the form shows the reason and stays disabled', async ({
    page,
    request,
    scratch,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'scratch-dep', SAMPLE_A);
    const service = await createService(request, deployment, 'cache');
    await page.goto(`/services/${service}`);
    await enableControls(page);
    await settings(page).locator('#cmds').fill('docker compose up -d');
    await settings(page).locator('#timeout').fill('90');

    const answer = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/services/${service}/postdeploy`) &&
        response.request().method() === 'POST',
    );
    await settings(page).getByRole('button', { name: 'Save settings' }).click();
    expect((await answer).status()).toBe(422);

    await expect(settings(page).locator('p[role="alert"]')).toHaveText(
      `${UNAVAILABLE} Post-deploy commands are off, so settings cannot be saved.`,
    );
    await expect(settings(page).locator('#cmds')).toHaveValue('docker compose up -d');
    await expect(settings(page).locator('#cmds')).toBeDisabled();
    await expect(settings(page).getByRole('button', { name: 'Save settings' })).toBeDisabled();
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);

    await page.reload();
    await expect(settings(page).locator('#cmds')).toHaveValue('');
    await expect(panel(page)).toContainText('No deploys yet.');
  });

  test('a compose check sent anyway answers that Docker is unusable and runs nothing', async ({
    page,
    request,
    scratch,
  }) => {
    const deployment = await createDeployment(request, scratch.id, 'scratch-dep', SAMPLE_A);
    const service = await createService(request, deployment, 'cache');
    await page.goto(`/services/${service}`);
    await enableControls(page);
    await settings(page).locator('#wd').fill(SAMPLE_A);
    await settings(page).getByRole('button', { name: 'Test command' }).click();

    const result = settings(page).locator('#test-result');
    await expect(result).toContainText(
      `${UNAVAILABLE} Post-deploy commands are off, so the check cannot run.`,
    );
    await expect(result.locator('[role="note"]')).toBeVisible();
    await expect(result.locator('pre')).toHaveCount(0);
    await expect(result).toHaveAttribute('aria-live', 'polite');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the panel fragment keeps the form and the status line', async ({ page, browser }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);
    const response = await page.request.get(`/services/${id}/postdeploy`, {
      headers: { 'HX-Request': 'true' },
    });
    expect(response.status()).toBe(200);
    expect(response.headers()['cache-control']).toBe('no-store');
    const html = await response.text();
    expect(html).toContain('data-fragment="docker-status"');
    expect(html).toContain('data-fragment="postdeploy-settings"');
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('sees neither the status line nor the form and may not save or test', async ({
    page,
    browser,
  }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);
    await expect(panel(page).locator('[data-fragment="docker-status"]')).toHaveCount(0);
    await expect(panel(page).locator('[data-fragment="postdeploy-settings"]')).toHaveCount(0);
    await expect(panel(page)).not.toContainText('Docker socket');
    await expect(panel(page)).not.toContainText(STORED_COMMAND);

    const headers = await csrfHeaders(page.request);
    const save = await page.request.post(`/services/${id}/postdeploy`, {
      form: { commands: 'docker ps', timeout: '30' },
      headers,
    });
    const check = await page.request.post(`/services/${id}/postdeploy/test`, {
      form: { working_directory: '' },
      headers,
    });
    expect([save.status(), check.status()]).toEqual([403, 403]);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('sees status only and may not save or test', async ({ page, browser }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);
    await expect(panel(page).locator('[data-fragment="docker-status"]')).toHaveCount(0);
    await expect(panel(page).locator('[data-fragment="postdeploy-settings"]')).toHaveCount(0);
    await expect(panel(page)).not.toContainText('Docker socket');
    await expect(panel(page).getByRole('button')).toHaveCount(0);

    const headers = await csrfHeaders(page.request);
    const save = await page.request.post(`/services/${id}/postdeploy`, {
      form: { commands: 'docker ps', timeout: '30' },
      headers,
    });
    const check = await page.request.post(`/services/${id}/postdeploy/test`, {
      form: { working_directory: '' },
      headers,
    });
    expect([save.status(), check.status()]).toEqual([403, 403]);
  });
});

test.describe('environment grant', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('sees no settings and the endpoints answer 403', async ({ page, browser }) => {
    const id = await prodMysql(browser);
    await page.goto(`/services/${id}`);
    await expect(panel(page).locator('[data-fragment="docker-status"]')).toHaveCount(0);
    await expect(panel(page).locator('[data-fragment="postdeploy-settings"]')).toHaveCount(0);
    await expect(panel(page)).not.toContainText('Docker socket');

    const headers = await csrfHeaders(page.request);
    const save = await page.request.post(`/services/${id}/postdeploy`, {
      form: { commands: 'docker ps', timeout: '30' },
      headers,
    });
    expect(save.status()).toBe(403);
  });
});

test.describe('deployment grant', () => {
  test.use({ storageState: stateFile('deploymentAdmin') });

  test('sees the output and no settings', async ({ page, browser }) => {
    const id = await prodMysql(browser);
    await page.goto(`/services/${id}`);
    await expect(panel(page).locator('[data-fragment="postdeploy-settings"]')).toHaveCount(0);
    await expect(panel(page)).toContainText(
      'Command text and settings are visible to the superadmin only.',
    );
  });
});

test.describe('no access', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('save and test answer 404 for a service outside the scope', async ({ page, browser }) => {
    const id = await prodMysql(browser);
    const headers = await csrfHeaders(page.request);
    const save = await page.request.post(`/services/${id}/postdeploy`, {
      form: { commands: 'docker ps', timeout: '30' },
      headers,
    });
    const check = await page.request.post(`/services/${id}/postdeploy/test`, {
      form: { working_directory: '' },
      headers,
    });
    expect([save.status(), check.status()]).toEqual([404, 404]);
  });
});
