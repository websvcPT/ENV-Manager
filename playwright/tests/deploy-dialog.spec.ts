import type { APIRequestContext, Locator, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment, saveRaw } from '../environments-api';
import { stateFile } from '../global-setup';
import { createHostFolder, type HostFolder } from '../host-files';
import { csrfHeaders } from '../projects-api';
import { createService, serviceId } from '../services-api';
import { expect, test as scratchTest } from './scratch-project';

/*
 * The deploy dialog against the default stack, which has no Docker socket: pre-flight outcomes,
 * selection, force with its confirmation, every result state that needs no daemon, "Deploy this
 * version" from the history, and what each role may do. Every spec roots its deployment in a
 * folder of its own below /srv/deployments (removed afterwards), because a deploy really writes
 * the files. Specs never open the dialog on seeded services: a deploy or a dry run there would
 * change the seeded state other specs read. Results that need a daemon (the post-deploy command
 * ran or failed) are covered by playwright/docker-tests.
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

interface World {
  service: number;
  deployment: number;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/** A deployment rooted in the host folder with one service. */
async function world(
  request: APIRequestContext,
  projectId: number,
  host: HostFolder,
  policy: 'block' | 'warn' = 'block',
): Promise<World> {
  const deployment = await createDeployment(request, projectId, 'deploy', host.root, policy);
  const service = await createService(request, deployment, 'web');
  return { service, deployment };
}

function dialog(page: Page): Locator {
  return page.locator('#deploy-dialog');
}

function row(page: Page, name: string): Locator {
  return dialog(page).locator(`[data-fragment="preflight-row"][data-env="${name}"]`);
}

function submit(page: Page): Locator {
  return dialog(page).locator('[data-deploy-submit]');
}

/** Open the dialog from the service page and wait for the pre-flight. */
async function openDialog(page: Page, service: number): Promise<Locator> {
  await page.goto(`/services/${service}`);
  await page.getByRole('button', { name: 'Deploy service' }).click();
  await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
  return dialog(page);
}

function banner(page: Page): Locator {
  return dialog(page).locator('[data-fragment="deploy-result"]');
}

function resultRow(page: Page, name: string): Locator {
  return dialog(page).locator(`[data-fragment="result-row"][data-env="${name}"]`);
}

async function expectFitsViewport(page: Page): Promise<void> {
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  const box = await dialog(page).boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.width + 1);
}

test.describe('pre-flight', () => {
  test('lists the files with the dry run outcome of each and preselects the ones to deploy', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    host.write('environments/.env.block-dist', 'NEED_KEY=\n');
    host.write('environments/.env.drift', 'ON_DISK=1\n');
    host.write('environments/.env.same', 'SAME=1\n');
    await createEnvironment(request, service, '.env.new', 'NEW=1\n');
    await createEnvironment(request, service, '.env.same', 'SAME=1\n');
    await createEnvironment(request, service, '.env.drift', 'MANAGED=1\n');
    await createEnvironment(
      request,
      service,
      '.env.block',
      'OTHER=1\n',
      'environments/.env.block-dist',
    );

    await openDialog(page, service);
    await expect(dialog(page).locator('#dd-title')).toHaveText('Deploy web');
    await expect(dialog(page).locator('[aria-current="step"]')).toContainText('Pre-flight');

    const names = dialog(page).locator('[data-fragment="preflight-row"]');
    await expect(names).toHaveCount(4);
    await expect(row(page, '.env.new').locator('[data-status]')).toHaveAttribute(
      'data-status',
      'proceed',
    );
    await expect(row(page, '.env.new')).toContainText('Drift: Target missing');
    await expect(row(page, '.env.new')).toContainText('Skipped, no dist file linked');
    await expect(row(page, '.env.same').locator('[data-status]')).toHaveAttribute(
      'data-status',
      'unchanged',
    );
    await expect(row(page, '.env.drift').locator('[data-status]')).toHaveAttribute(
      'data-status',
      'halt',
    );
    await expect(row(page, '.env.drift')).toContainText('Drift: Target changed outside the app');
    await expect(row(page, '.env.block').locator('[data-status]')).toHaveAttribute(
      'data-status',
      'block',
    );
    await expect(row(page, '.env.block')).toContainText('1 missing key, policy: block');

    for (const name of ['.env.new', '.env.same', '.env.drift', '.env.block']) {
      await expect(row(page, name).locator('input[type="checkbox"]')).toBeChecked();
    }
    await expect(dialog(page).locator('[data-fragment="preflight-summary"]')).toHaveText(
      'Dry run: 1 environment would be written, 0 warnings, 1 blocked, 1 halted, 1 unchanged. ' +
        'Nothing has been changed yet.',
    );
    await expect(submit(page)).toHaveText('Deploy 4 environments');
    await expect(dialog(page).locator('[data-fragment="post-deploy-preview"]')).toContainText(
      'This service has no post-deploy command.',
    );
    await expect(dialog(page).locator('[data-fragment="post-deploy-preview"] pre')).toHaveCount(0);
    await expect(dialog(page).getByText('Force overwrite')).toBeVisible();
    await expect(dialog(page).locator('input[name="force"]')).not.toBeChecked();
    await expectFitsViewport(page);

    expect(host.list('environments').filter((name) => !name.endsWith('-dist'))).toEqual([
      '.env.drift',
      '.env.same',
    ]);
  });

  test('a file whose dist keys are missing reads Will warn under the warn policy', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host, 'warn');
    host.write('environments/.env.app-dist', 'APP=\nMISSING_ONE=\nMISSING_TWO=\n');
    await createEnvironment(request, service, '.env.app', 'APP=1\n', 'environments/.env.app-dist');

    await openDialog(page, service);
    await expect(row(page, '.env.app').locator('[data-status]')).toHaveAttribute(
      'data-status',
      'warnout',
    );
    await expect(row(page, '.env.app')).toContainText('2 missing keys, policy: warn');
    await expect(dialog(page).locator('[data-fragment="preflight-summary"]')).toContainText(
      'Dry run: 1 environment would be written, 1 warning, 0 blocked.',
    );
  });

  test('a file that cannot be deployed reads Will fail with the reason', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.app', 'APP=1\n');
    host.removeBelow('environments');
    await openDialog(page, service);
    await expect(row(page, '.env.app').locator('[data-status="failed"]')).toHaveText('Will fail');
    await expect(row(page, '.env.app')).toContainText('Drift: Not checked');
    await expect(dialog(page).locator('[data-fragment="preflight-summary"]')).toContainText(
      '1 would fail',
    );
  });

  test('changing the choice updates the button and marks the dry run as out of date', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.one', 'ONE=1\n');
    await createEnvironment(request, service, '.env.two', 'TWO=1\n');
    await openDialog(page, service);

    const note = dialog(page).locator('[data-stale-note]');
    await expect(note).toBeHidden();
    await expect(submit(page)).toHaveText('Deploy 2 environments');

    await row(page, '.env.two').locator('input[type="checkbox"]').uncheck();
    await expect(submit(page)).toHaveText('Deploy 1 environment');
    await expect(note).toBeVisible();
    await expect(note).toContainText('The selection changed after the dry run.');

    await row(page, '.env.one').locator('input[type="checkbox"]').uncheck();
    await expect(submit(page)).toBeDisabled();
    await expect(submit(page)).toHaveText('Deploy');

    await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-summary"]')).toHaveText(
      'No environment is selected. Choose the environments to deploy.',
    );
    await expect(row(page, '.env.one').locator('input[type="checkbox"]')).not.toBeChecked();
    await expect(note).toBeHidden();
    await expect(submit(page)).toBeDisabled();
  });

  test('the dialog opens again with fresh content and closes with Cancel', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.one', 'ONE=1\n');
    await openDialog(page, service);
    await dialog(page).getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog(page)).toBeHidden();
    await page.getByRole('button', { name: 'Deploy service' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    await dialog(page).getByRole('button', { name: 'Close', exact: true }).first().click();
    await expect(dialog(page)).toBeHidden();
  });

  test('a pre-flight that fails closes the dialog, tells the person and works again later', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.one', 'ONE=1\n');
    await page.goto(`/services/${service}`);
    await page.route('**/deploy/preflight', (route) => route.fulfill({ status: 500, body: '' }));
    await page.getByRole('button', { name: 'Deploy service' }).click();
    await expect(dialog(page)).toBeHidden();
    await expect(page.locator('[data-fragment="toast"]')).toContainText('The request failed.');

    await page.unroute('**/deploy/preflight');
    await page.getByRole('button', { name: 'Deploy service' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    await expect(row(page, '.env.one')).toBeVisible();
  });

  test('the Deploy buttons of the deployment page open the dialog for their service', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service, deployment } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.one', 'ONE=1\n');
    await page.goto(`/deployments/${deployment}`);
    await page.locator('#services').getByRole('button', { name: 'Deploy' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    await expect(dialog(page).locator('#dd-title')).toHaveText('Deploy web');
    await expect(row(page, '.env.one')).toBeVisible();
  });
});

test.describe('deploying', () => {
  test('a clean deploy writes the chosen files, shows the result and refreshes the panels', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    host.write('environments/.env.same', 'SAME=1\n');
    await createEnvironment(request, service, '.env.new', 'NEW=1\n');
    await createEnvironment(request, service, '.env.same', 'SAME=1\n');
    await openDialog(page, service);
    await expect(page.locator('#environments [data-status="never"]:visible')).toHaveCount(2);

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok');
    await expect(banner(page)).toContainText('Success');
    await expect(banner(page)).toContainText('1 environment written. No post-deploy command is set.');
    await expect(dialog(page).locator('[aria-current="step"]')).toContainText('Result');
    await expect(resultRow(page, '.env.new').locator('[data-status="success"]')).toHaveText(
      'Success',
    );
    await expect(resultRow(page, '.env.new')).toContainText('Created environments/.env.new');
    await expect(resultRow(page, '.env.same').locator('[data-status="unchanged"]')).toBeVisible();
    await expect(resultRow(page, '.env.same')).toContainText('Already matches this version');
    await expect(dialog(page).locator('[data-fragment="post-deploy-result"]')).toContainText(
      'This service has no post-deploy command.',
    );
    await expect(dialog(page).getByRole('link', { name: /^View audit entry #\d+$/ })).toHaveAttribute(
      'href',
      /^\/audit\?entry=\d+$/,
    );
    await expectFitsViewport(page);
    expect(host.read('environments/.env.new')).toBe('NEW=1\n');
    expect(host.list('environments').sort()).toEqual(['.env.new', '.env.same']);

    await dialog(page).getByRole('button', { name: 'Close', exact: true }).last().click();
    await expect(dialog(page)).toBeHidden();
    await expect(page.locator('#environments [data-status="deployed"]:visible')).toHaveCount(2);
    await expect(page.locator('#environments [data-status="never"]:visible')).toHaveCount(0);
    await expect(page.locator('#hist [data-fragment="history-row"]')).toHaveCount(1);
  });

  test('a drifted file halts the deploy, links the drift view and is left as it is', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    host.write('environments/.env.drift', 'ON_DISK=1\n');
    const drift = await createEnvironment(request, service, '.env.drift', 'MANAGED=1\n');
    await createEnvironment(request, service, '.env.new', 'NEW=1\n');
    await openDialog(page, service);

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'warn');
    await expect(banner(page)).toContainText('Halted: drift');
    await expect(banner(page)).toContainText(
      '.env.drift changed outside the app. The drift policy is halt, so it was not written. ' +
        '1 other environment written.',
    );
    await expect(resultRow(page, '.env.drift').locator('[data-status="halted"]')).toBeVisible();
    await expect(resultRow(page, '.env.new').locator('[data-status="success"]')).toBeVisible();
    await expect(dialog(page).locator('[data-fragment="post-deploy-result"]')).toBeVisible();
    await expect(dialog(page).getByRole('link', { name: 'Open drift view' })).toHaveAttribute(
      'href',
      `/environments/${drift}/drift`,
    );
    expect(host.read('environments/.env.drift')).toBe('ON_DISK=1\n');
    expect(host.read('environments/.env.new')).toBe('NEW=1\n');
    await expectFitsViewport(page);
  });

  test('missing dist keys block the deploy and link the dist report', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    host.write('environments/.env.block-dist', 'NEED_ONE=\nNEED_TWO=\n');
    const blocked = await createEnvironment(
      request,
      service,
      '.env.block',
      'OTHER=1\n',
      'environments/.env.block-dist',
    );
    await openDialog(page, service);

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'err');
    await expect(banner(page)).toContainText('Blocked: missing keys');
    await expect(banner(page)).toContainText(
      '.env.block is missing 2 keys from its dist file. Policy is block. Nothing was written.',
    );
    await expect(resultRow(page, '.env.block')).toContainText('Missing NEED_ONE, NEED_TWO');
    await expect(resultRow(page, '.env.block').locator('[data-status="blocked"]')).toBeVisible();
    await expect(dialog(page).getByRole('link', { name: 'Open dist report' })).toHaveAttribute(
      'href',
      `/environments/${blocked}/dist`,
    );
    expect(host.list('environments')).toEqual(['.env.block-dist']);
    await expectFitsViewport(page);
  });

  test('a file that cannot be written fails the deploy and offers a retry', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.app', 'APP=1\n');
    await openDialog(page, service);
    host.removeBelow('environments');

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'err');
    await expect(banner(page)).toContainText('Failed');
    await expect(banner(page)).toContainText('.env.app could not be written.');
    await expect(resultRow(page, '.env.app').locator('[data-status="failed"]')).toBeVisible();
    await expect(dialog(page).getByRole('button', { name: 'Retry deploy' })).toBeVisible();

    await dialog(page).getByRole('button', { name: 'Retry deploy' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    await expect(row(page, '.env.app').locator('[data-status="failed"]')).toHaveText('Will fail');
  });

  test('deploying nothing is refused in the dialog', async ({ page, request, scratch, host }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.app', 'APP=1\n');
    await openDialog(page, service);
    await row(page, '.env.app').locator('input[type="checkbox"]').uncheck();
    await submit(page).evaluate((button: HTMLButtonElement) => {
      button.disabled = false;
    });
    await submit(page).click();
    await expect(dialog(page).locator('[role="note"]')).toContainText(
      'Select at least one environment to deploy.',
    );
    await dialog(page).getByRole('button', { name: 'Back to pre-flight' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    expect(host.list('environments')).toEqual([]);
  });

  test('the deploy request is given a timeout far above the default of one minute', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    await createEnvironment(request, service, '.env.app', 'APP=1\n');
    await openDialog(page, service);
    await page.evaluate(() => {
      const seen: Array<{ source: string; timeout: number }> = [];
      (window as unknown as { requests: typeof seen }).requests = seen;
      document.addEventListener('htmx:before:request', (event) => {
        const detail = (event as CustomEvent).detail as {
          ctx?: { sourceElement?: Element; request?: { timeout?: unknown } };
        };
        seen.push({
          source: detail.ctx?.sourceElement?.id ?? '',
          timeout: Number(detail.ctx?.request?.timeout ?? 0),
        });
      });
    });

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok');
    const requests = await page.evaluate(
      () => (window as unknown as { requests: Array<{ source: string; timeout: number }> }).requests,
    );
    const deployRequests = requests.filter((entry) => entry.source === 'deploy-form');
    expect(deployRequests).toHaveLength(1);
    expect(deployRequests[0].timeout).toBeGreaterThan(3_600_000);
  });
});

test.describe('force overwrite', () => {
  test('is confirmed first, then replaces the drifted file after a backup', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    host.write('environments/.env.drift', 'ON_DISK=1\n');
    await createEnvironment(request, service, '.env.drift', 'MANAGED=1\n');
    await openDialog(page, service);
    await expect(row(page, '.env.drift').locator('[data-status="halt"]')).toBeVisible();

    await dialog(page).getByText('Force overwrite').first().click();
    await expect(dialog(page).locator('input[name="force"]')).toBeChecked();
    await submit(page).click();
    await expect(dialog(page).locator('#force-form')).toBeVisible();
    await expect(dialog(page).locator('[role="alert"]')).toContainText(
      'Force overwrite deploys .env.drift past the block policy for missing dist keys and the halt policy for drift.',
    );
    expect(host.read('environments/.env.drift')).toBe('ON_DISK=1\n');

    await dialog(page).getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    await expect(dialog(page).locator('input[name="force"]')).toBeChecked();
    expect(host.read('environments/.env.drift')).toBe('ON_DISK=1\n');

    await submit(page).click();
    await dialog(page).getByRole('button', { name: 'Force deploy 1 environment' }).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok');
    await expect(banner(page)).toContainText('Success');
    await expect(resultRow(page, '.env.drift')).toContainText(
      'previous file backed up, forced past the checks',
    );
    expect(host.read('environments/.env.drift')).toBe('MANAGED=1\n');
    const backups = host.list('environments').filter((name) => name.includes('.bak-'));
    expect(backups).toHaveLength(1);
    expect(host.read(`environments/${backups[0]}`)).toBe('ON_DISK=1\n');
    await expectFitsViewport(page);
  });

  test('past a dist block the dry run reads Will warn', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    host.write('environments/.env.block-dist', 'NEED_KEY=\n');
    await createEnvironment(
      request,
      service,
      '.env.block',
      'OTHER=1\n',
      'environments/.env.block-dist',
    );
    await openDialog(page, service);
    await expect(row(page, '.env.block').locator('[data-status="block"]')).toBeVisible();
    await dialog(page).locator('input[name="force"]').check();
    await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
    await expect(row(page, '.env.block').locator('[data-status="warnout"]')).toBeVisible();
    await expect(dialog(page).locator('input[name="force"]')).toBeChecked();
  });
});

test.describe('deploy this version', () => {
  test('the history deploys the chosen older version and says it is not the newest', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const { service } = await world(request, scratch.id, host);
    const environment = await createEnvironment(request, service, '.env.hist', 'V=1\n');
    await saveRaw(request, environment, 1, 'V=2\n');
    await saveRaw(request, environment, 2, 'V=3\n');
    await createEnvironment(request, service, '.env.other', 'OTHER=1\n');

    await page.goto(`/environments/${environment}/history`);
    await expect(page.getByRole('button', { name: 'Deploy v3' })).toBeVisible();
    await page.getByLabel('Compare version 1').check();
    await expect(page.locator('#dt')).toHaveText('Comparing v1 to v2');
    await page.getByRole('button', { name: 'Deploy v2' }).click();

    await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
    await expect(dialog(page).locator('[role="note"]').first()).toHaveText(
      'Deploying version 2 of .env.hist, which is not the newest version (v3).',
    );
    await expect(row(page, '.env.hist').locator('input[type="checkbox"]')).toBeChecked();
    await expect(row(page, '.env.other').locator('input[type="checkbox"]')).not.toBeChecked();
    await expect(row(page, '.env.hist')).toContainText('v2');
    await expectFitsViewport(page);

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok');
    await expect(resultRow(page, '.env.hist')).toContainText('v2');
    expect(host.read('environments/.env.hist')).toBe('V=2\n');
    expect(host.list('environments')).toEqual(['.env.hist']);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('has no deploy action and the server refuses both requests', async ({
    page,
    request,
    browser,
  }) => {
    const service = await serviceId(browser, 'rentals', 'prod', 'mysql');
    await page.goto(`/services/${service}`);
    await expect(page.locator('[data-fragment="page-header"]')).toBeVisible();
    await expect(page.getByRole('button', { name: /Deploy/ })).toHaveCount(0);
    await expect(page.locator('#deploy-dialog')).toHaveCount(0);

    const headers = await csrfHeaders(request);
    for (const path of ['deploy/preflight', 'deploy']) {
      const response = await request.post(`/services/${service}/${path}`, {
        form: { selection: 'explicit', environment: '1' },
        headers,
      });
      expect(response.status()).toBe(403);
    }
  });
});

test.describe('scoped viewer', () => {
  test.use({ storageState: stateFile('envScopedViewer') });

  test('has no deploy action and the server refuses both requests', async ({
    page,
    request,
    browser,
  }) => {
    const service = await serviceId(browser, 'rentals', 'prod', 'mysql');
    await page.goto(`/services/${service}`);
    await expect(page.getByRole('button', { name: /Deploy/ })).toHaveCount(0);
    const headers = await csrfHeaders(request);
    for (const path of ['deploy/preflight', 'deploy']) {
      const response = await request.post(`/services/${service}/${path}`, {
        form: { selection: 'explicit', environment: '1' },
        headers,
      });
      expect(response.status()).toBe(403);
    }
  });
});

test.describe('admin without a grant', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('gets 404 for the service and for both requests', async ({ page, request, browser }) => {
    const service = await serviceId(browser, 'rentals', 'prod', 'mysql');
    const response = await page.goto(`/services/${service}`);
    expect(response?.status()).toBe(404);
    const headers = await csrfHeaders(request);
    for (const path of ['deploy/preflight', 'deploy']) {
      const posted = await request.post(`/services/${service}/${path}`, {
        form: { selection: 'explicit', environment: '1' },
        headers,
      });
      expect(posted.status()).toBe(404);
    }
  });
});

test.describe('admin roles', () => {
  for (const [label, user] of [
    ['admin', 'admin'],
    ['scoped admin', 'envScopedAdmin'],
  ] as const) {
    test.describe(label, () => {
      test.use({ storageState: stateFile(user) });

      test(`sees the Deploy action on a service in scope`, async ({ page, browser }) => {
        const service = await serviceId(browser, 'rentals', 'prod', 'mysql');
        await page.goto(`/services/${service}`);
        await expect(page.getByRole('button', { name: 'Deploy service' })).toBeVisible();
        await expect(page.locator('#deploy-dialog')).toHaveCount(1);
        await expect(page.locator('#deploy-dialog')).toBeHidden();
      });
    });
  }
});
