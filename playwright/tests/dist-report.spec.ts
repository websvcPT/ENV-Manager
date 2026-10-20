import type { APIRequestContext, Browser, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { createEnvironment, saveRaw, seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';
import { csrfHeaders } from '../http';
import { expectItemHeader, pageWithoutLocation } from '../item-header';
import { createService } from '../services-api';
import { horizontalOverflow, isWide } from '../viewport';
import { rowAction } from './row-actions';
import { expect, test } from './scratch-project';

const SAMPLE_A = '/srv/deployments/sample-a';
const BLOCKED_DIST = 'environments/e2e-blocked-dist';
const MATCHING_DIST = 'environments/e2e-matching-dist';
const TEXT_DIST = 'environments/e2e-text-dist';
const DIST_DEFAULT_VALUE = 'e2e-dist-default-Hv51';
const ENV_VALUE = 'e2e-env-value-Rt62';
const PEM = '-----BEGIN CERTIFICATE-----\nE2EDISTBODY\n-----END CERTIFICATE-----\n';
// The blocked dist template defines DB_HOST, DB_PORT, DB_NAME and DB_USER; this file lacks two.
const ENV_TEXT = `DB_HOST=${ENV_VALUE}\nDB_EXTRA=1\nDB_USER=\n`;
const COMPLETE_TEXT = `DB_HOST=${ENV_VALUE}\nDB_PORT=1\nDB_NAME=a\nDB_USER=b\n`;
const STALE = 'This file changed since the check. The report below is up to date.';

interface Prepared {
  environment: number;
  service: number;
  deployment: number;
}

/** A deployment on the sample folder with one service, as the superadmin. */
async function deploymentWithService(
  request: APIRequestContext,
  projectId: number,
  policy: 'block' | 'warn' = 'block',
): Promise<{ deployment: number; service: number }> {
  const unique = Math.random().toString(36).slice(2, 8);
  const deployment = await createDeployment(request, projectId, `dep-${unique}`, SAMPLE_A, policy);
  const service = await createService(request, deployment, 'cache');
  return { deployment, service };
}

/** One environment with this content and dist link in a fresh deployment. */
async function prepare(
  request: APIRequestContext,
  projectId: number,
  name: string,
  content: string,
  distPath: string,
  policy: 'block' | 'warn' = 'block',
): Promise<Prepared> {
  const { deployment, service } = await deploymentWithService(request, projectId, policy);
  const environment = await createEnvironment(request, service, name, content, distPath);
  return { environment, service, deployment };
}

function header(page: Page) {
  return page.locator('[data-fragment="page-header"]');
}

function report(page: Page) {
  return page.locator('#dist-report');
}

function summary(page: Page) {
  return report(page).locator('[aria-label="Summary"] li');
}

function tables(page: Page) {
  return report(page).locator('[data-fragment="dist-table"]');
}

function addButton(page: Page) {
  return report(page).locator('button[hx-post*="/add-missing"]');
}

/** The file rows of the deployment overview: table rows from md, list items below. */
function fileRows(page: Page) {
  return page.locator(
    isWide(page) ? '[data-fragment="dist-row"]' : '[data-fragment="dist-item"]',
  );
}

function toast(page: Page) {
  return page.locator('[data-fragment="toast"]');
}

async function expectNoValueInPage(page: Page, ...values: string[]): Promise<void> {
  const html = await pageWithoutLocation(page);
  for (const value of values) {
    expect(html).not.toContain(value);
  }
}

test.describe('superadmin report', () => {
  test('opens from the service page and lists key names, lines and counts under block', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment, service } = await prepare(
      request,
      scratch.id,
      '.env.blocked',
      ENV_TEXT,
      BLOCKED_DIST,
    );
    await page.goto(`/services/${service}`);
    const row = page.locator('[data-fragment="env-row"]:visible', { hasText: '.env.blocked' });
    await (await rowAction(row, 'dist')).click();
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}/dist$`));
    await expectItemHeader(page, 'environment', '.env.blocked');
    await expect(header(page).locator('[data-status="blocked"]')).toHaveText('Blocked');
    await expect(header(page)).toContainText(`Dist file ${BLOCKED_DIST}`);
    await expect(header(page)).toContainText(/checked \d{4}-\d\d-\d\d \d\d:\d\d by root/);
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
      'Edit',
      'History',
      'Compare to dist',
      'AccessSuperadmin',
    ]);
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'Compare to dist',
    );
    await expect(report(page).locator('[role="alert"]')).toContainText('Blocked: keys missing');
    await expect(report(page)).toContainText(
      'Policy is block. A deploy of this environment stops until the missing keys are added.',
    );
    await expect(summary(page)).toHaveText([/2\s*Missing keys/, /1\s*Extra keys/, /1\s*Empty values/]);
    await expect(tables(page)).toHaveCount(3);
    await expect(tables(page).nth(0).locator('tbody tr')).toHaveText([
      /DB_PORT\s*Line 3/,
      /DB_NAME\s*Line 4/,
    ]);
    await expect(tables(page).nth(1).locator('tbody tr')).toHaveText(
      /DB_EXTRA\s*Not in dist\. Allowed, no policy applies\./,
    );
    await expect(tables(page).nth(2).locator('tbody tr')).toHaveText(
      /DB_USER\s*Value is empty in the environment\./,
    );
    await expect(addButton(page)).toHaveText('Add missing keys to the editor');
    await expectNoValueInPage(page, DIST_DEFAULT_VALUE, ENV_VALUE, SAMPLE_A);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the warn policy shows a warning and keeps the add action', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment } = await prepare(
      request,
      scratch.id,
      '.env.warned',
      ENV_TEXT,
      BLOCKED_DIST,
      'warn',
    );
    await page.goto(`/environments/${environment}/dist`);
    await expect(header(page).locator('[data-status="drift"]')).toHaveText('Warning');
    await expect(report(page).locator('[role="alert"]')).toContainText('Warning: keys missing');
    await expect(report(page)).toContainText(
      'Policy is warn and continue. A deploy proceeds and reports the missing keys.',
    );
    await expect(summary(page).first()).toContainText('2');
    await expect(addButton(page)).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a file with every key is a passed check without tables', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment } = await prepare(
      request,
      scratch.id,
      '.env.passed',
      COMPLETE_TEXT,
      MATCHING_DIST,
    );
    await page.goto(`/environments/${environment}/dist`);
    await expect(header(page).locator('[data-status="proceed"]')).toHaveText('Passed');
    await expect(report(page).locator('[role="status"]')).toContainText('No keys missing');
    await expect(summary(page)).toHaveText([/0\s*Missing keys/, /2\s*Extra keys/, /0\s*Empty values/]);
    await expect(tables(page)).toHaveCount(1);
    await expect(addButton(page)).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('skips the comparison for a certificate, an unlinked file and a text dist file', async ({
    page,
    request,
    scratch,
  }) => {
    const { service } = await deploymentWithService(request, scratch.id);
    const certificate = await createEnvironment(request, service, 'e2e-ca.pem', PEM);
    const unlinked = await createEnvironment(request, service, '.env.unlinked', 'A=1\n');
    const textDist = await createEnvironment(request, service, '.env.textdist', 'A=1\n', TEXT_DIST);

    await page.goto(`/environments/${certificate}/dist`);
    await expect(header(page).locator('[data-status="never"]')).toHaveText('Skipped');
    await expect(header(page)).toContainText('Dist file none linked');
    await expect(report(page).locator('[role="note"]')).toHaveText(
      'This file is not env format (certificate). Key comparison is skipped for it.',
    );
    await expect(summary(page)).toHaveCount(0);
    await expect(addButton(page)).toHaveCount(0);
    await expectNoValueInPage(page, 'E2EDISTBODY', 'BEGIN CERTIFICATE');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.goto(`/environments/${unlinked}/dist`);
    await expect(header(page).locator('[data-status="never"]')).toHaveText('Skipped');
    await expect(report(page).locator('[role="note"]')).toHaveText(
      'No dist file is linked to this file. Key comparison is skipped for it.',
    );

    await page.goto(`/environments/${textDist}/dist`);
    await expect(header(page)).toContainText(`Dist file ${TEXT_DIST}`);
    await expect(report(page).locator('[role="note"]')).toHaveText(
      'The dist file is not env format. Key comparison is skipped for it.',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a dist file that cannot be read is a failed check with fixed wording', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment } = await prepare(
      request,
      scratch.id,
      '.env.failed',
      ENV_TEXT,
      'environments/e2e-nonexistent-dist',
    );
    await page.goto(`/environments/${environment}/dist`);
    await expect(header(page).locator('[data-status="failed"]')).toHaveText('Check failed');
    await expect(report(page).locator('[role="alert"]')).toContainText('The check could not run');
    await expect(report(page)).toContainText('The dist file does not exist or is not a regular file.');
    await expect(summary(page)).toHaveCount(0);
    await expect(addButton(page)).toHaveCount(0);
    await expectNoValueInPage(page, SAMPLE_A, '/srv/deployments');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('running the check again refreshes the report and the header without a reload', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment } = await prepare(
      request,
      scratch.id,
      '.env.again',
      ENV_TEXT,
      BLOCKED_DIST,
    );
    await page.goto(`/environments/${environment}/dist`);
    await expect(header(page).locator('[data-status="blocked"]')).toBeVisible();
    await page.evaluate(() => {
      (window as unknown as { marker: number }).marker = 1;
    });
    await saveRaw(request, environment, 1, COMPLETE_TEXT);
    await header(page).getByRole('button', { name: 'Run check again' }).click();
    await expect(header(page).locator('[data-status="proceed"]')).toHaveText('Passed');
    await expect(report(page).locator('[role="status"]')).toContainText('No keys missing');
    await expect(header(page)).toContainText(/checked \d{4}-\d\d-\d\d \d\d:\d\d by root/);
    expect(await page.evaluate(() => (window as unknown as { marker?: number }).marker)).toBe(1);
    await expect(page.locator('[data-fragment="page-header"]')).toHaveCount(1);
    await expect(page.locator('#dist-report')).toHaveCount(1);
  });

  test('adds the missing keys as a new version through the editor save', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment } = await prepare(
      request,
      scratch.id,
      '.env.add',
      ENV_TEXT,
      BLOCKED_DIST,
    );
    await page.goto(`/environments/${environment}/dist`);
    await addButton(page).click();
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}/dist\\?added=2&version=2$`));
    await expect(toast(page)).toContainText(
      'Added 2 missing keys with empty values as version 2. Pending deploy.',
    );
    await expect(header(page).locator('[data-status="proceed"]')).toHaveText('Passed');
    await expect(summary(page)).toHaveText([/0\s*Missing keys/, /1\s*Extra keys/, /3\s*Empty values/]);
    await expect(tables(page).last().locator('tbody tr th')).toHaveText([
      'DB_USER',
      'DB_PORT',
      'DB_NAME',
    ]);
    await expect(addButton(page)).toHaveCount(0);
    await expectNoValueInPage(page, DIST_DEFAULT_VALUE, ENV_VALUE);

    await page.goto(`/environments/${environment}/history`);
    await expect(page.locator('[data-fragment="version-row"]')).toHaveCount(2);
    await expect(page.locator('[data-fragment="version-row"]').first()).toContainText(
      'Added missing keys from the dist file',
    );
    await page.goto(`/environments/${environment}`);
    await expect(header(page)).toContainText('Version 2 saved');
    await expect(page.locator('main')).toContainText('DB_PORT');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('refuses to add keys to a report that is out of date and shows a fresh one', async ({
    page,
    request,
    scratch,
  }) => {
    const { environment } = await prepare(
      request,
      scratch.id,
      '.env.stale',
      ENV_TEXT,
      BLOCKED_DIST,
    );
    await page.goto(`/environments/${environment}/dist`);
    await saveRaw(request, environment, 1, `${ENV_TEXT}# changed elsewhere\n`);
    await addButton(page).click();
    await expect(report(page).locator('p[role="alert"]')).toHaveText(STALE);
    await expect(report(page).locator('[role="alert"]').nth(1)).toContainText(
      'Blocked: keys missing',
    );
    await expect(addButton(page)).toHaveAttribute(
      'hx-post',
      `/environments/${environment}/add-missing?base=2`,
    );
    await expect(page).toHaveURL(new RegExp(`/environments/${environment}/dist$`));
    await page.goto(`/environments/${environment}/history`);
    await expect(page.locator('[data-fragment="version-row"]')).toHaveCount(2);
    await expect(page.locator('main')).not.toContainText('Added missing keys from the dist file');
    await page.goto(`/environments/${environment}/dist`);
    await addButton(page).click();
    await expect(toast(page)).toContainText('as version 3');
  });
});

test.describe('deployment overview', () => {
  test('checks every file of a deployment and links to each report', async ({
    page,
    request,
    scratch,
  }) => {
    const { deployment, service } = await deploymentWithService(request, scratch.id);
    const blocked = await createEnvironment(request, service, '.env.one', ENV_TEXT, BLOCKED_DIST);
    await createEnvironment(request, service, '.env.two', COMPLETE_TEXT, MATCHING_DIST);
    await createEnvironment(request, service, 'e2e-ca.pem', PEM);
    await createEnvironment(request, service, '.env.gone', 'A=1\n', 'environments/e2e-nonexistent-dist');

    await page.goto(`/projects/${scratch.id}`);
    const deploymentRow = page.locator('[data-fragment="deployment-row"]');
    await (await rowAction(deploymentRow, 'dist')).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${deployment}/dist$`));
    await expect(header(page).locator('h1')).toHaveText('Dist check');
    const rows = fileRows(page);
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(0)).toContainText('.env.gone');
    await expect(rows.nth(0).locator('[data-status="failed"]')).toBeVisible();
    await expect(rows.nth(1)).toContainText('.env.one');
    await expect(rows.nth(1).locator('[data-status="blocked"]')).toBeVisible();
    await expect(rows.nth(2)).toContainText('.env.two');
    await expect(rows.nth(2).locator('[data-status="proceed"]')).toBeVisible();
    await expect(rows.nth(3)).toContainText('e2e-ca.pem');
    await expect(rows.nth(3).locator('[data-status="never"]')).toBeVisible();
    if (isWide(page)) {
      await expect(rows.nth(1).locator('td').nth(2)).toHaveText('2');
    } else {
      await expect(rows.nth(1)).toContainText('2 missing · 1 extra · 1 empty');
    }
    await expectNoValueInPage(page, DIST_DEFAULT_VALUE, ENV_VALUE, 'E2EDISTBODY', SAMPLE_A);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.goto(`/deployments/${deployment}`);
    await page.getByRole('link', { name: 'Check dist' }).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${deployment}/dist$`));
    await rows.nth(1).getByRole('link', { name: 'Open report' }).click();
    await expect(page).toHaveURL(new RegExp(`/environments/${blocked}/dist$`));
    await expect(header(page).locator('[data-status="blocked"]')).toBeVisible();
  });

  test('says so when the deployment has no file to check and offers no row action', async ({
    page,
    request,
    scratch,
  }) => {
    const { deployment } = await deploymentWithService(request, scratch.id);
    await page.goto(`/projects/${scratch.id}`);
    await expect(page.locator(`a[href="/deployments/${deployment}"]`).first()).toBeVisible();
    await expect(page.locator(`a[href="/deployments/${deployment}/dist"]`)).toHaveCount(0);
    await page.goto(`/deployments/${deployment}/dist`);
    await expect(header(page).locator('h1')).toHaveText('Dist check');
    await expect(page.locator('[data-fragment="empty-state"]')).toContainText('No files to check');
    await expect(fileRows(page)).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('refuses to check a deployment outside the allowed paths', async ({ page, browser }) => {
    const legacy = await deploymentId(browser, 'billing', 'legacy');
    await page.goto(`/deployments/${legacy}`);
    await expect(page.getByRole('button', { name: 'Check dist' })).toBeDisabled();
    await page.goto(`/deployments/${legacy}/dist`);
    await expect(page.locator('main')).toContainText('This deployment is outside the allowed paths.');
    await expect(fileRows(page)).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Run check again' })).toHaveCount(0);
  });
});

async function seededDb(browser: Browser): Promise<number> {
  return seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
}

async function seededEditor(browser: Browser): Promise<number> {
  return seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
}

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('has no Compare to dist tab and the server refuses every dist request', async ({
    page,
    browser,
  }) => {
    const id = await seededDb(browser);
    const prod = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    await page.goto(`/environments/${id}`);
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText(['View', 'History']);
    expect((await page.request.get(`/environments/${id}/dist`)).status()).toBe(403);
    expect((await page.request.get(`/deployments/${prod}/dist`)).status()).toBe(403);
    const headers = await csrfHeaders(page.request);
    expect((await page.request.post(`/environments/${id}/dist-check`, { headers })).status()).toBe(
      403,
    );
    expect(
      (await page.request.post(`/environments/${id}/add-missing?base=2`, { headers })).status(),
    ).toBe(403);
    await page.goto(`/deployments/${prod}`);
    await expect(page.getByRole('link', { name: 'Check dist' })).toHaveCount(0);
  });
});

test.describe('other roles', () => {
  test('an admin gets the report; a seeded file whose dist is not on disk fails safely', async ({
    browser,
  }) => {
    const id = await seededDb(browser);
    const context = await browser.newContext({ storageState: stateFile('admin') });
    const page = await context.newPage();
    try {
      expect((await page.goto(`/environments/${id}/dist`))?.status()).toBe(200);
      await expectItemHeader(page, 'environment', '.env.db');
      await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
        'Edit',
        'History',
        'Compare to dist',
      ]);
      await expect(report(page)).toContainText('The check could not run');
      await expect(addButton(page)).toHaveCount(0);
      await expectNoValueInPage(page, '/opt/fancy');
    } finally {
      await context.close();
    }
  });

  test('an environment scoped admin checks its file only and never sees a root path', async ({
    browser,
  }) => {
    const owned = await seededDb(browser);
    const other = await seededEditor(browser);
    const prod = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    const context = await browser.newContext({ storageState: stateFile('envScopedAdmin') });
    const page = await context.newPage();
    try {
      expect((await page.goto(`/environments/${owned}/dist`))?.status()).toBe(200);
      await expect(report(page)).toContainText('The check could not run');
      await expectNoValueInPage(page, '/opt/fancy', '/srv/deployments');
      expect((await page.goto(`/environments/${other}/dist`))?.status()).toBe(404);
      expect((await page.goto(`/deployments/${prod}/dist`))?.status()).toBe(200);
      await expect(fileRows(page)).toHaveCount(1);
      await expect(fileRows(page)).toContainText('.env.db');
      await expect(page.locator('main')).not.toContainText('.env.db.app');
      await expect(page.locator('main')).not.toContainText('.env.db.secrets');
      await expectNoValueInPage(page, '/opt/fancy');
    } finally {
      await context.close();
    }
  });

  test('a scoped viewer is refused and an admin without grants gets 404', async ({ browser }) => {
    const id = await seededDb(browser);
    const prod = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    const viewer = await browser.newContext({ storageState: stateFile('envScopedViewer') });
    const denied = await viewer.newPage();
    try {
      expect((await denied.goto(`/environments/${id}/dist`))?.status()).toBe(403);
    } finally {
      await viewer.close();
    }
    const outsider = await browser.newContext({ storageState: stateFile('ungrantedAdmin') });
    const page = await outsider.newPage();
    try {
      expect((await page.goto(`/environments/${id}/dist`))?.status()).toBe(404);
      expect((await page.goto(`/deployments/${prod}/dist`))?.status()).toBe(404);
      const headers = await csrfHeaders(page.request);
      const check = await page.request.post(`/environments/${id}/dist-check`, { headers });
      expect(check.status()).toBe(404);
      const add = await page.request.post(`/environments/${id}/add-missing?base=2`, { headers });
      expect(add.status()).toBe(404);
    } finally {
      await outsider.close();
    }
  });
});
