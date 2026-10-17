import type { APIRequestContext, Browser, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { stateFile } from '../global-setup';
import { createService, serviceId } from '../services-api';
import { expect, test } from './scratch-project';

const SAMPLE_A = '/srv/deployments/sample-a';
const OUTSIDE = 'Resolves outside the deployment root and the allowed paths.';

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

interface Prepared {
  deployment: number;
  service: number;
}

/** A deployment on the sample folder with one service, inside the throwaway project. */
async function prepare(
  request: APIRequestContext,
  projectId: number,
  serviceName = 'cache',
): Promise<Prepared> {
  const deployment = await createDeployment(request, projectId, 'scratch-dep', SAMPLE_A);
  const service = await createService(request, deployment, serviceName);
  return { deployment, service };
}

function form(page: Page) {
  return page.locator('#env-form');
}

function header(page: Page) {
  return page.locator('[data-fragment="page-header"]');
}

function toast(page: Page) {
  return page.locator('[data-fragment="toast"]');
}

function environmentLink(page: Page, name: string) {
  return page
    .locator('#environments')
    .getByRole('link', { name, exact: true })
    .filter({ visible: true });
}

async function fillFile(page: Page, name: string, target: string): Promise<void> {
  await page.locator('#en').fill(name);
  await page.locator('#tp').fill(target);
}

async function submit(page: Page): Promise<void> {
  await form(page).getByRole('button', { name: 'Add environment' }).click();
}

async function labelFill(page: Page, id: string): Promise<string> {
  return page
    .locator(`#${id}`)
    .locator('xpath=ancestor::label')
    .evaluate((label) => getComputedStyle(label).backgroundColor);
}

async function seededMysql(browser: Browser): Promise<number> {
  return serviceId(browser, 'rentals', 'prod', 'mysql');
}

test.describe('superadmin add service', () => {
  test('shows the empty form', async ({ page, request, scratch }) => {
    const { deployment } = await prepare(request, scratch.id);
    await page.goto(`/deployments/${deployment}/services/new`);
    await expect(header(page).locator('h1')).toHaveText('Add service');
    await expect(header(page)).toContainText('Superadmin');
    await expect(page.locator('nav[aria-label="Breadcrumb"] li')).toHaveText([
      'Projects',
      '/',
      scratch.name,
      '/',
      'scratch-dep',
      '/',
      'Add service',
    ]);
    const serviceForm = page.locator('#service-form');
    await expect(serviceForm).toHaveAttribute('hx-post', `/deployments/${deployment}/services/new`);
    await expect(serviceForm.locator('[role="note"]')).toContainText('A service groups');
    await expect(page.locator('#svc-name')).toHaveValue('');
    await expect(page.locator('#svc-name-h')).toHaveText('Use the Compose service name.');
    await expect(page.getByRole('link', { name: 'Cancel' })).toHaveAttribute(
      'href',
      `/deployments/${deployment}`,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('refuses an empty, a badly formed and a duplicate name', async ({
    page,
    request,
    scratch,
  }) => {
    const { deployment } = await prepare(request, scratch.id);
    await page.goto(`/deployments/${deployment}/services/new`);
    const add = page.locator('#service-form').getByRole('button', { name: 'Add service' });
    const error = page.locator('#svc-name-e');

    await add.click();
    await expect(error).toHaveText('Enter a service name.');
    await expect(page.locator('#svc-name')).toHaveAttribute('aria-invalid', 'true');

    await page.locator('#svc-name').fill('Bad Name');
    await add.click();
    await expect(error).toContainText('Use 1 to 100 lowercase letters');
    await expect(page.locator('#svc-name')).toHaveValue('Bad Name');

    await page.locator('#svc-name').fill('cache');
    await add.click();
    await expect(error).toHaveText('A service named cache already exists in this deployment.');
    await expect(page).toHaveURL(/\/services\/new$/);
  });

  test('creates the service and lands on its page with the add prompt', async ({
    page,
    request,
    scratch,
  }) => {
    const { deployment } = await prepare(request, scratch.id);
    await page.goto(`/deployments/${deployment}/services/new`);
    await page.locator('#svc-name').fill('redis');
    await page.locator('#service-form').getByRole('button', { name: 'Add service' }).click();
    await expect(page).toHaveURL(/\/services\/\d+\?added=service$/);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('redis');
    await expect(toast(page)).toContainText('Service added');
    await expect(page.locator('#environments [data-fragment="empty-state"]')).toContainText(
      'No environments yet',
    );
    const serviceId = Number(page.url().match(/\/services\/(\d+)/)?.[1]);
    await expect(
      page.locator('#environments').getByRole('link', { name: 'Add environment' }).last(),
    ).toHaveAttribute('href', `/services/${serviceId}/environments/new`);
  });

  test('cancel returns to the deployment', async ({ page, request, scratch }) => {
    const { deployment } = await prepare(request, scratch.id);
    await page.goto(`/deployments/${deployment}/services/new`);
    await page.getByRole('link', { name: 'Cancel' }).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${deployment}$`));
  });

  test('the deployment page links to the form', async ({ page, request, scratch }) => {
    const { deployment } = await prepare(request, scratch.id);
    await page.goto(`/deployments/${deployment}`);
    await page.getByRole('link', { name: 'Add service' }).first().click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${deployment}/services/new$`));
  });
});

test.describe('superadmin add environment', () => {
  test('shows the empty form with its sections', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await request.post(`/deployments/${service}/services/new`).catch(() => undefined);
    await page.goto(`/services/${service}`);
    await page.locator('#environments').getByRole('link', { name: 'Add environment' }).last().click();
    await expect(page).toHaveURL(new RegExp(`/services/${service}/environments/new$`));
    await expect(header(page).locator('h1')).toHaveText('Add environment');
    await expect(header(page)).toContainText('Superadmin');
    await expect(header(page).locator('p')).toHaveText('Superadmin only.');
    await expect(page.locator('nav[aria-label="Breadcrumb"] li')).toHaveText([
      'Projects',
      '/',
      scratch.name,
      '/',
      'scratch-dep',
      '/',
      'cache',
      '/',
      'Add environment',
    ]);
    await expect(form(page)).toHaveAttribute('hx-post', `/services/${service}/environments/new`);
    await expect(form(page).locator('[data-fragment="form-section"] h2')).toHaveText([
      'Service',
      'File',
      'Initial content',
    ]);
    await expect(page.locator('#svc option:checked')).toHaveText('cache');
    await expect(page.locator('#svc option')).toHaveText(['cache', 'Create a new service…']);
    await expect(page.locator('#nsvc')).toHaveValue('');
    await expect(page.locator('#en')).toHaveValue('');
    await expect(page.locator('#tp')).toHaveValue('');
    await expect(page.locator('#tp-h')).toHaveText(`Resolves to ${SAMPLE_A}/`);
    await expect(page.locator('#dp-h')).toHaveText('Read from the target for comparison. Never written.');
    await expect(page.locator('#m')).toHaveAttribute('placeholder', '0600');
    await expect(page.locator('#o')).toHaveAttribute('placeholder', 'app user');
    await expect(page.locator('input[name="source"]:checked')).toHaveValue('paste');
    await expect(page.locator('#content-h')).toHaveText(
      'Values are masked in the editor after saving.',
    );
    await expect(page.getByRole('link', { name: 'Cancel' })).toHaveAttribute(
      'href',
      `/services/${service}`,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('switching the source shows the matching input', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    const content = page.locator('#content-field');
    const upload = page.locator('#upload-field');
    await expect(content).toBeVisible();
    await expect(upload).toBeHidden();
    const pasteFill = await labelFill(page, 'src-paste');
    expect(pasteFill).not.toBe(await labelFill(page, 'src-upload'));

    await page.locator('#src-upload').check();
    await expect(content).toBeHidden();
    await expect(upload).toBeVisible();
    await expect(page.locator('#upload-h')).toContainText('256 KiB');
    expect(await labelFill(page, 'src-upload')).toBe(pasteFill);
    expect(await labelFill(page, 'src-paste')).not.toBe(pasteFill);

    await page.locator('#src-import').check();
    await expect(content).toBeHidden();
    await expect(upload).toBeHidden();

    await page.locator('#src-paste').check();
    await expect(content).toBeVisible();
    await expect(upload).toBeHidden();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('shows where the target resolves as the person types', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await page.locator('#tp').fill('environments/.env.cache');
    await expect(page.locator('#tp-h')).toHaveText(
      `Resolves to ${SAMPLE_A}/environments/.env.cache`,
    );
    await page.locator('#tp').fill('../../etc/cache.env');
    await expect(page.locator('#tp-e')).toHaveText(OUTSIDE);
    await expect(page.locator('#tp-e')).toHaveAttribute('role', 'alert');
    await page.locator('#tp').fill('escape.conf');
    await expect(page.locator('#tp-e')).toHaveText(OUTSIDE);
    await page.locator('#tp').fill('/etc/passwd');
    await expect(page.locator('#tp-e')).toHaveText('Use a path relative to the deployment root.');
    await page.locator('#tp').fill('nested');
    await expect(page.locator('#tp-e')).toHaveText('Target path is a directory, not a file.');
  });

  test('refuses bad values and keeps what was typed', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, 'bad name', '../../etc/cache.env');
    await page.locator('#dp').fill('../x');
    await page.locator('#m').fill('999');
    await page.locator('#o').fill('bad owner');
    await page.locator('#content').fill('KEEP_ME=1\nSECOND=2');
    await submit(page);

    await expect(page).toHaveURL(new RegExp(`/services/${service}/environments/new$`));
    await expect(page.locator('#en-e')).toContainText('Use 1 to 100 letters');
    await expect(page.locator('#en')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#tp-e')).toHaveText(OUTSIDE);
    await expect(page.locator('#tp')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#tp')).toHaveClass(/border-danger/);
    await expect(page.locator('#dp-e')).toHaveText(OUTSIDE);
    await expect(page.locator('#m-e')).toHaveText('Use an octal mode such as 0600.');
    await expect(page.locator('#o-e')).toContainText('Owner must be');
    await expect(page.locator('#en')).toHaveValue('bad name');
    await expect(page.locator('#tp')).toHaveValue('../../etc/cache.env');
    await expect(page.locator('#content')).toHaveValue('KEEP_ME=1\nSECOND=2');
    await expect(toast(page)).toHaveCount(0);
    if ((page.viewportSize()?.width ?? 0) < 768) {
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    }
  });

  test('pasted text creates the environment with version 1', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, '.env.cache', 'environments/.env.cache');
    await page.locator('#dp').fill('environments/.env.cache-dist');
    await page.locator('#content').fill('CACHE_HOST=\nCACHE_PORT=6379');
    await submit(page);

    await expect(page).toHaveURL(new RegExp(`/services/${service}\\?added=environment$`));
    await expect(toast(page)).toContainText('Environment added');
    const link = environmentLink(page, '.env.cache');
    await expect(link).toBeVisible();
    const row = page
      .locator('#environments')
      .locator('tr[data-env=".env.cache"], li[data-fragment="env-row"]')
      .filter({ visible: true });
    await expect(row).toContainText('environments/.env.cache');
    await expect(row).toContainText('v1');
    await expect(row).toContainText(/Dist\s+linked|Linked/);
    await expect(row).toContainText(/Never deployed/);
  });

  test('refuses a second file with the same name or target', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, '.env.cache', 'environments/.env.cache');
    await submit(page);
    await expect(page).toHaveURL(/added=environment/);

    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, '.env.cache', 'environments/other');
    await submit(page);
    await expect(page.locator('#en-e')).toHaveText(
      'This service already has a file named .env.cache.',
    );

    await fillFile(page, '.env.other', 'environments/.env.cache');
    await submit(page);
    await expect(page.locator('#tp-e')).toHaveText(
      'Another file of this deployment already uses this target path.',
    );
  });

  test('a new service is created inline with the file', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await page.locator('#svc').selectOption({ label: 'Create a new service…' });
    await page.locator('#nsvc').fill('Bad Name');
    await fillFile(page, 'redis.conf', 'redis.conf');
    await submit(page);
    await expect(page.locator('#nsvc-e')).toContainText('Use 1 to 100 lowercase letters');

    await page.locator('#nsvc').fill('redis');
    await submit(page);
    await expect(page).toHaveURL(/\/services\/\d+\?added=environment$/);
    expect(page.url()).not.toContain(`/services/${service}?`);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('redis');
    await expect(environmentLink(page, 'redis.conf')).toBeVisible();
  });

  test('choosing "Create a new service" without a name asks for one', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await page.locator('#svc').selectOption({ label: 'Create a new service…' });
    await fillFile(page, 'x.conf', 'x.conf');
    await submit(page);
    await expect(page.locator('#nsvc-e')).toHaveText('Enter a service name.');
  });

  test('an uploaded text file becomes version 1', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, 'uploaded.env', 'uploaded.env');
    await page.locator('#src-upload').check();

    await submit(page);
    await expect(page.locator('#upload-e')).toHaveText('Choose a text file to upload.');
    await expect(page.locator('input[name="source"]:checked')).toHaveValue('upload');

    await page.locator('#upload').setInputFiles({
      name: 'binary.bin',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from([0xff, 0xfe, 0x00, 0x41]),
    });
    await submit(page);
    await expect(page.locator('#upload-e')).toHaveText('The file is not valid UTF-8 text.');

    await page.locator('#upload').setInputFiles({
      name: 'uploaded.env',
      mimeType: 'text/plain',
      buffer: Buffer.from('UPLOADED=1\n'),
    });
    await submit(page);
    await expect(page).toHaveURL(new RegExp(`/services/${service}\\?added=environment$`));
    await expect(environmentLink(page, 'uploaded.env')).toBeVisible();
  });

  test('imports the current target file as version 1', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, 'imported.env', 'import-source.env');
    await page.locator('#src-import').check();
    await submit(page);
    await expect(page).toHaveURL(new RegExp(`/services/${service}\\?added=environment$`));
    const row = page
      .locator('#environments')
      .locator('tr[data-env="imported.env"], li[data-fragment="env-row"]')
      .filter({ visible: true });
    await expect(row).toContainText('import-source.env');
    await expect(row).toContainText('v1');
  });

  test('import refuses a missing file and a symlink that leaves the allowed paths', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await page.locator('#src-import').check();

    await fillFile(page, 'missing.env', 'not-there.env');
    await submit(page);
    await expect(page.locator('#env-form [role="alert"]').filter({ hasText: 'does not exist' }))
      .toHaveText('The target file does not exist or is not a regular file.');

    await fillFile(page, 'escape.conf', 'escape.conf');
    await submit(page);
    await expect(page.locator('#tp-e')).toHaveText(OUTSIDE);
    await expect(
      page.locator('#env-form [role="alert"]').filter({ hasText: 'Fix the target path' }),
    ).toHaveText('Fix the target path to import the file.');
    await expect(page.locator('#en')).toHaveValue('escape.conf');
    await expect(page.locator('input[name="source"]:checked')).toHaveValue('import');
    await page.goto(`/services/${service}`);
    await expect(environmentLink(page, 'escape.conf')).toHaveCount(0);
  });

  test('refuses an oversized paste', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await fillFile(page, 'big.env', 'big.env');
    await page.locator('#content').fill('A'.repeat(256 * 1024 + 1));
    await submit(page);
    await expect(page.locator('#content-e')).toHaveText('Content is larger than 256 KiB.');
  });

  test('cancel returns to the service page', async ({ page, request, scratch }) => {
    const { service } = await prepare(request, scratch.id);
    await page.goto(`/services/${service}/environments/new`);
    await page.getByRole('link', { name: 'Cancel' }).click();
    await expect(page).toHaveURL(new RegExp(`/services/${service}$`));
  });
});

for (const user of ['admin', 'viewer'] as const) {
  test.describe(`${user} cannot add services or environments`, () => {
    test.use({ storageState: stateFile(user) });

    test('both forms answer 403', async ({ browser, page }) => {
      const mysql = await seededMysql(browser);
      const prod = await deploymentId(browser, 'rentals', 'prod');
      for (const url of [
        `/services/${mysql}/environments/new`,
        `/deployments/${prod}/services/new`,
      ]) {
        const response = await page.goto(url);
        expect(response?.status()).toBe(403);
        await expect(page.locator('#env-form')).toHaveCount(0);
        await expect(page.locator('#service-form')).toHaveCount(0);
      }
    });

    test('the pages show no add actions', async ({ browser, page }) => {
      const mysql = await seededMysql(browser);
      const prod = await deploymentId(browser, 'rentals', 'prod');
      await page.goto(`/services/${mysql}`);
      await expect(page.getByRole('link', { name: 'Add environment' })).toHaveCount(0);
      await page.goto(`/deployments/${prod}`);
      await expect(page.getByRole('link', { name: 'Add service' })).toHaveCount(0);
    });
  });
}
