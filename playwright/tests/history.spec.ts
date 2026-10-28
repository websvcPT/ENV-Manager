import type { APIRequestContext, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment, saveRaw, seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';
import { expectItemHeader } from '../item-header';
import { csrfHeaders } from '../http';
import { createService } from '../services-api';
import { horizontalOverflow } from '../viewport';
import { expect, test } from './scratch-project';
import {
  asRolesWithoutAccess,
  expectNoValueInPage,
  SEEDED_SECRETS,
  seededEditor,
} from './seeded-editor';

const SAMPLE_A = '/srv/deployments/sample-a';
const OLD_PASSWORD = 'e2e-old-Pw41';
const NEW_PASSWORD = 'e2e-new-Pw42';
const GONE_VALUE = 'e2e-gone-Lg43';
const ADDED_VALUE = 'e2e-added-Ad44';
const UNCHANGED_VALUE = 'db.unchanged.example.test';
const COMMENT_TEXT = 'e2e comment line';

const FIRST =
  `# ${COMMENT_TEXT}\n` +
  `HOST=${UNCHANGED_VALUE}\n` +
  `PASSWORD=${OLD_PASSWORD}\n` +
  `LEGACY=${GONE_VALUE}\n`;
const SECOND =
  `# ${COMMENT_TEXT}\n` +
  `HOST=${UNCHANGED_VALUE}\n` +
  `PASSWORD=${NEW_PASSWORD}\n` +
  `EXTRA=${ADDED_VALUE}\n`;

const PEM_OLD = '-----BEGIN CERTIFICATE-----\nE2EOLDBODY\n-----END CERTIFICATE-----\n';
const PEM_NEW = '-----BEGIN CERTIFICATE-----\nE2ENEWBODY\n-----END CERTIFICATE-----\n';

/** An environment with these versions, oldest first, inside the scratch project. */
async function prepare(
  request: APIRequestContext,
  projectId: number,
  name: string,
  versions: string[],
  fileType: 'env' | 'cert' = 'env',
): Promise<number> {
  const unique = Math.random().toString(36).slice(2, 8);
  const deployment = await createDeployment(request, projectId, `dep-${unique}`, SAMPLE_A);
  const service = await createService(request, deployment, 'cache');
  const id = await createEnvironment(request, service, name, versions[0], '', fileType);
  for (let index = 1; index < versions.length; index += 1) {
    await saveRaw(request, id, index, versions[index]);
  }
  return id;
}

function header(page: Page) {
  return page.locator('[data-fragment="page-header"]');
}

function table(page: Page) {
  return page.locator('[data-fragment="version-table"]');
}

function versionRow(page: Page, number: number) {
  return table(page).locator('[data-fragment="version-row"]', {
    has: page.locator(`input[name="v"][value="${number}"]`),
  });
}

function panel(page: Page) {
  return page.locator('[data-fragment="diff-panel"]');
}

function diffRow(page: Page, key: string) {
  return panel(page).locator('[data-fragment="diff-row"]', { hasText: key });
}

function revealButton(page: Page) {
  return panel(page).locator('button[hx-post*="/history/reveal"]');
}

function hideButton(page: Page) {
  return panel(page).locator('button[data-js="reveal-hide"]');
}

function restoreButton(page: Page) {
  return panel(page).locator('button[data-target="restore-dialog"]');
}

function toast(page: Page) {
  return page.locator('[data-fragment="toast"]');
}

async function checkedVersions(page: Page): Promise<string[]> {
  return table(page).locator('input[name="v"]:checked').evaluateAll((boxes) =>
    boxes.map((box) => (box as HTMLInputElement).value),
  );
}

test.describe('superadmin history', () => {
  test('lists the versions and the masked key level diff of the two newest', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.history', [FIRST, SECOND]);
    await page.goto(`/environments/${id}/history`);
    await expectItemHeader(page, 'environment', '.env.history');
    await expect(header(page)).toContainText('Version 2 saved, never deployed');
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
      'Edit',
      'History',
      'Compare to dist',
      'Access',
    ]);
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'History',
    );
    await expect(table(page).locator('[data-fragment="version-row"] th[scope="row"]')).toHaveText([
      'v2',
      'v1',
    ]);
    await expect(versionRow(page, 2)).toContainText('root');
    await expect(versionRow(page, 2)).toContainText('None');
    await expect(versionRow(page, 1)).toContainText('Initial content (paste)');
    await expect(versionRow(page, 2).locator('[data-status="deployed"]')).toHaveCount(0);
    expect(await checkedVersions(page)).toEqual(['2', '1']);
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v1 to v2');
    await expect(panel(page).locator('[data-fragment="diff-row"]')).toHaveCount(3);
    await expect(diffRow(page, 'PASSWORD').locator('[data-status="changed"]')).toBeVisible();
    await expect(diffRow(page, 'EXTRA').locator('[data-status="added"]')).toBeVisible();
    await expect(diffRow(page, 'EXTRA')).toContainText('not set');
    await expect(diffRow(page, 'LEGACY').locator('[data-status="removed"]')).toBeVisible();
    await expect(diffRow(page, 'LEGACY')).toContainText('removed');
    await expect(diffRow(page, 'PASSWORD')).toContainText('••••••••');
    await expect(panel(page)).toContainText('1 unchanged key hidden');
    await expect(revealButton(page)).toBeVisible();
    await expect(hideButton(page)).toBeHidden();
    await expect(restoreButton(page)).toHaveText('Restore v1 as new version');
    await expectNoValueInPage(
      page,
      OLD_PASSWORD,
      NEW_PASSWORD,
      GONE_VALUE,
      ADDED_VALUE,
      UNCHANGED_VALUE,
      COMMENT_TEXT,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('choosing two versions loads their diff and a third choice drops the earliest', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.pick', ['A=1\nB=1\n', 'A=2\nB=1\n', 'A=2\nB=1\nC=3\n']);
    await page.goto(`/environments/${id}/history`);
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v2 to v3');
    await expect(diffRow(page, 'C').locator('[data-status="added"]')).toBeVisible();
    await expect(versionRow(page, 3)).toHaveClass(/has-checked:bg-accent-soft/);
    const hitArea = versionRow(page, 1).locator('label');
    const box = await hitArea.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
    await hitArea.click({ position: { x: 3, y: 3 } });
    expect((await checkedVersions(page)).sort()).toEqual(['1', '2']);
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v1 to v2');
    await expect(diffRow(page, 'A').locator('[data-status="changed"]')).toBeVisible();
    await expect(panel(page).locator('[data-fragment="diff-row"]')).toHaveCount(1);
    await versionRow(page, 2).locator('input[name="v"]').uncheck();
    await expect(panel(page).locator('h2#dt')).toHaveText('Compare versions');
    await expect(panel(page)).toContainText('Select two versions in the table to compare them.');
    await expect(revealButton(page)).toHaveCount(0);
    await expect(restoreButton(page)).toHaveCount(0);
    await versionRow(page, 3).locator('input[name="v"]').check();
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v1 to v3');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('reveals the changed values only, audited, and removes them from the page', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.reveal', [FIRST, SECOND]);
    await page.goto(`/environments/${id}/history`);
    await revealButton(page).click();
    await expect(panel(page)).toHaveAttribute('data-revealed', 'true');
    await expect(diffRow(page, 'PASSWORD').locator('[data-reveal-value]')).toHaveText([
      OLD_PASSWORD,
      NEW_PASSWORD,
    ]);
    await expect(diffRow(page, 'EXTRA').locator('[data-reveal-value]')).toHaveText(ADDED_VALUE);
    await expect(diffRow(page, 'LEGACY').locator('[data-reveal-value]')).toHaveText(GONE_VALUE);
    await expect(panel(page).locator('[data-reveal-value]').first()).toHaveAttribute(
      'hx-ignore',
      '',
    );
    await expect(revealButton(page)).toBeHidden();
    await expect(hideButton(page)).toBeVisible();
    const masks = diffRow(page, 'PASSWORD').getByText('••••••••');
    await expect(masks).toHaveCount(2);
    await expect(masks.first()).toBeHidden();
    await expect(masks.last()).toBeHidden();
    await expectNoValueInPage(page, UNCHANGED_VALUE, COMMENT_TEXT);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await hideButton(page).click();
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, OLD_PASSWORD, NEW_PASSWORD, GONE_VALUE, ADDED_VALUE);
    await revealButton(page).click();
    await expect(panel(page)).toHaveAttribute('data-revealed', 'true');
    await expect(diffRow(page, 'EXTRA').locator('[data-reveal-value]')).toHaveText(ADDED_VALUE);
  });

  test('values hide by themselves after 30 seconds', async ({ page, request, scratch }) => {
    const id = await prepare(request, scratch.id, '.env.timer', [FIRST, SECOND]);
    await page.clock.install();
    await page.goto(`/environments/${id}/history`);
    await revealButton(page).click();
    await expect(diffRow(page, 'EXTRA').locator('[data-reveal-value]')).toHaveText(ADDED_VALUE);
    await page.clock.fastForward(29_000);
    await expect(panel(page)).toHaveAttribute('data-revealed', 'true');
    await page.clock.fastForward(2_000);
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, OLD_PASSWORD, NEW_PASSWORD, GONE_VALUE, ADDED_VALUE);
  });

  test('choosing another pair clears revealed values', async ({ page, request, scratch }) => {
    const id = await prepare(request, scratch.id, '.env.clear', [FIRST, SECOND, FIRST]);
    await page.goto(`/environments/${id}/history`);
    await revealButton(page).click();
    await expect(panel(page)).toHaveAttribute('data-revealed', 'true');
    await versionRow(page, 1).locator('input[name="v"]').check();
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expectNoValueInPage(page, OLD_PASSWORD, NEW_PASSWORD, GONE_VALUE, ADDED_VALUE);
  });

  test('restore asks for confirmation and saves the old content as a new version', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.restore', [FIRST, SECOND]);
    await page.goto(`/environments/${id}/history`);
    await restoreButton(page).click();
    const dialog = page.locator('#restore-dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('h2')).toHaveText('Restore v1 as a new version');
    await expect(dialog).toContainText(
      'Creates version 3 with the content of version 1. Version 2 stays in the history. The restored version is not deployed until you deploy it.',
    );
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();
    await expect(versionRow(page, 3)).toHaveCount(0);
    await restoreButton(page).click();
    await dialog.getByRole('button', { name: 'Restore as new version' }).click();
    await expect(page).toHaveURL(/\/history\?restored=3&source=1$/);
    await expect(toast(page)).toContainText('Restored version 1 as version 3. Pending deploy.');
    await expect(header(page)).toContainText('Version 3 saved');
    await expect(table(page).locator('[data-fragment="version-row"] th[scope="row"]')).toHaveText([
      'v3',
      'v2',
      'v1',
    ]);
    await expect(versionRow(page, 3)).toContainText('Restored from version 1');
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v2 to v3');
    await expect(diffRow(page, 'LEGACY').locator('[data-status="added"]')).toBeVisible();
    await page.goto(`/environments/${id}`);
    await expect(header(page)).toContainText('Version 3 saved');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('restore is refused for a page that is out of date and for the current content', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.refuse', [FIRST, SECOND, FIRST]);
    await page.goto(`/environments/${id}/history`);
    await versionRow(page, 2).locator('input[name="v"]').uncheck();
    await versionRow(page, 1).locator('input[name="v"]').check();
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v1 to v3');
    await expect(panel(page)).toContainText('These versions have the same content.');
    await restoreButton(page).click();
    const dialog = page.locator('#restore-dialog');
    await dialog.getByRole('button', { name: 'Restore as new version' }).click();
    await expect(dialog.locator('[role="alert"]')).toContainText(
      'Version 1 has the same content as the current version. Nothing to restore.',
    );
    await expect(dialog).toBeVisible();
    await saveRaw(request, id, 3, SECOND);
    await dialog.getByRole('button', { name: 'Restore as new version' }).click();
    await expect(dialog.locator('[role="alert"]')).toContainText(
      'This file changed since you opened it. Reload the page to continue.',
    );
    await page.goto(`/environments/${id}/history`);
    await expect(table(page).locator('[data-fragment="version-row"] th[scope="row"]')).toHaveText([
      'v4',
      'v3',
      'v2',
      'v1',
    ]);
  });

  test('says so when the versions differ only in comments and when a file has one version', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.comments', [
      'A=1\n',
      '# a note\nA=1\n',
    ]);
    await page.goto(`/environments/${id}/history`);
    await expect(panel(page)).toContainText('No variables differ.');
    await expect(panel(page)).toContainText('only in comments, blank lines or order.');
    await expect(revealButton(page)).toHaveCount(0);
    await expect(panel(page)).toContainText('1 unchanged key hidden');
    const single = await prepare(request, scratch.id, '.env.single', ['A=1\n']);
    await page.goto(`/environments/${single}/history`);
    await expect(table(page).locator('[data-fragment="version-row"]')).toHaveCount(1);
    await expect(panel(page)).toContainText(
      'This file has one version. Save another version to compare.',
    );
    await expect(restoreButton(page)).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a certificate is compared line by line and revealed as the differing lines', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, 'ca.pem', [PEM_OLD, PEM_NEW], 'cert');
    await page.goto(`/environments/${id}/history`);
    await expect(panel(page).locator('.bg-head')).toContainText('Line');
    await expect(diffRow(page, 'Line 2').locator('[data-status="changed"]')).toBeVisible();
    await expect(panel(page).locator('[data-fragment="diff-row"]')).toHaveCount(1);
    await expect(panel(page)).toContainText('2 unchanged lines hidden');
    await expectNoValueInPage(page, 'E2EOLDBODY', 'E2ENEWBODY', 'BEGIN CERTIFICATE');
    await revealButton(page).click();
    await expect(diffRow(page, 'Line 2').locator('[data-reveal-value]')).toHaveText([
      'E2EOLDBODY',
      'E2ENEWBODY',
    ]);
    await hideButton(page).click();
    await expectNoValueInPage(page, 'E2EOLDBODY', 'E2ENEWBODY');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a line inserted in a certificate shows as one added line', async ({
    page,
    request,
    scratch,
  }) => {
    const before = '-----BEGIN CERTIFICATE-----\nE2EONE\nE2ETWO\nE2ETHREE\n-----END CERTIFICATE-----\n';
    const after =
      '-----BEGIN CERTIFICATE-----\nE2EONE\nE2EINSERTED\nE2ETWO\nE2ETHREE\n-----END CERTIFICATE-----\n';
    const id = await prepare(request, scratch.id, 'ins.pem', [before, after], 'cert');
    await page.goto(`/environments/${id}/history`);
    await expect(panel(page).locator('[data-fragment="diff-row"]')).toHaveCount(1);
    await expect(diffRow(page, 'Line 3').locator('[data-status="added"]')).toBeVisible();
    await expect(panel(page)).toContainText('5 unchanged lines hidden');
    await revealButton(page).click();
    await expect(diffRow(page, 'Line 3').locator('[data-reveal-value]')).toContainText(
      'E2EINSERTED',
    );
  });

  test('free text in an ENV file is compared as one file and revealed as whole texts', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, 'free.txt', [PEM_OLD, PEM_NEW]);
    await page.goto(`/environments/${id}/history`);
    await expect(diffRow(page, 'File content').locator('[data-status="changed"]')).toBeVisible();
    await expectNoValueInPage(page, 'E2EOLDBODY', 'E2ENEWBODY', 'BEGIN CERTIFICATE');
    await revealButton(page).click();
    await expect(diffRow(page, 'File content').locator('[data-reveal-value]')).toHaveText([
      PEM_OLD.trimEnd(),
      PEM_NEW.trimEnd(),
    ]);
    await hideButton(page).click();
    await expectNoValueInPage(page, 'E2EOLDBODY', 'E2ENEWBODY');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('marks the deployed version of a seeded file', async ({ page, browser }) => {
    const id = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'staging', 'web', '.env.web.tuning');
    await page.goto(`/environments/${id}/history`);
    await expect(versionRow(page, 1).locator('[data-status="deployed"]')).toBeVisible();
    await expect(versionRow(page, 2).locator('[data-status="deployed"]')).toHaveCount(0);
    await expect(header(page)).toContainText('Version 2 saved, version 1 deployed');
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('sees the versions and a masked diff with no reveal or restore', async ({
    page,
    browser,
  }) => {
    const id = await seededEditor(browser);
    await page.goto(`/environments/${id}/history`);
    await expectItemHeader(page, 'environment', '.env.db.app');
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText(['View', 'History']);
    await expect(table(page).locator('[data-fragment="version-row"]')).toHaveCount(3);
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v2 to v3');
    await expect(panel(page)).toContainText('3 unchanged keys hidden. Values are masked for the Viewer group.');
    await expect(diffRow(page, 'MAX_CONNECTIONS').locator('[data-status="changed"]')).toBeVisible();
    await expect(page.locator('main button[hx-post]')).toHaveCount(0);
    await expect(page.locator('#restore-dialog')).toHaveCount(0);
    await expect(panel(page).locator('button')).toHaveCount(0);
    await versionRow(page, 1).locator('input[name="v"]').check();
    await expect(panel(page).locator('h2#dt')).toHaveText('Comparing v1 to v2');
    await expect(panel(page).locator('button')).toHaveCount(0);
    await expectNoValueInPage(
      page,
      ...SEEDED_SECRETS,
      'db.internal.example.com',
      'rotated by hand',
      'BEGIN CERTIFICATE',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the server refuses reveal and restore requests', async ({ page, browser }) => {
    const id = await seededEditor(browser);
    await page.goto(`/environments/${id}/history`);
    const headers = await csrfHeaders(page.request);
    const revealed = await page.request.post(`/environments/${id}/history/reveal?v=2&v=3`, {
      headers,
    });
    expect(revealed.status()).toBe(403);
    expect(await revealed.text()).not.toContain('sentinelCertBody');
    const restored = await page.request.post(`/environments/${id}/restore/1?base=3`, { headers });
    expect(restored.status()).toBe(403);
  });
});

test.describe('other roles', () => {
  test('an admin gets the reveal and restore controls and stays masked', async ({ browser }) => {
    const id = await seededEditor(browser);
    const context = await browser.newContext({ storageState: stateFile('admin') });
    const page = await context.newPage();
    try {
      await page.goto(`/environments/${id}/history`);
      await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
        'Edit',
        'History',
        'Compare to dist',
      ]);
      await expect(revealButton(page)).toBeVisible();
      await expect(restoreButton(page)).toHaveText('Restore v2 as new version');
      await expect(page.locator('#restore-dialog')).toBeHidden();
      await expectNoValueInPage(page, ...SEEDED_SECRETS, 'db.internal.example.com');
    } finally {
      await context.close();
    }
  });

  test('an environment scoped admin reaches the history of its file only', async ({ browser }) => {
    const owned = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
    const other = await seededEditor(browser);
    const context = await browser.newContext({ storageState: stateFile('envScopedAdmin') });
    const page = await context.newPage();
    try {
      expect((await page.goto(`/environments/${owned}/history`))?.status()).toBe(200);
      await expect(table(page).locator('[data-fragment="version-row"]')).toHaveCount(2);
      await expect(revealButton(page)).toBeVisible();
      expect((await page.goto(`/environments/${other}/history`))?.status()).toBe(404);
    } finally {
      await context.close();
    }
  });

  test('an admin without grants and a scoped viewer get 404 for the history', async ({
    browser,
  }) => {
    const other = await seededEditor(browser);
    await asRolesWithoutAccess(browser, async (page) => {
      expect((await page.goto(`/environments/${other}/history`))?.status()).toBe(404);
      const headers = await csrfHeaders(page.request);
      const reveal = await page.request.post(`/environments/${other}/history/reveal?v=2&v=3`, {
        headers,
      });
      expect(reveal.status()).toBe(404);
      const restore = await page.request.post(`/environments/${other}/restore/1?base=3`, {
        headers,
      });
      expect(restore.status()).toBe(404);
    });
  });
});
