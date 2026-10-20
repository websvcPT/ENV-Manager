import type { APIRequestContext, Browser, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment, saveRaw, seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';
import { expectItemHeader, expectLocation } from '../item-header';
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
const SECRET = 'e2e-secret-Qx81';
const CERTIFICATE_BODY = 'E2ECERTBODY';
const CONTENT =
  '# Connection\n' +
  'HOST=db.example.test\n' +
  `PASSWORD=${SECRET}\n` +
  `CERT="-----BEGIN CERTIFICATE-----\n${CERTIFICATE_BODY}\n-----END CERTIFICATE-----"\n`;
const PEM = `-----BEGIN CERTIFICATE-----\n${CERTIFICATE_BODY}\n-----END CERTIFICATE-----\n`;

/** A deployment on the sample folder with one service and one file, inside the scratch project. */
async function prepare(
  request: APIRequestContext,
  projectId: number,
  name: string,
  content: string,
  fileType: 'env' | 'cert' = 'env',
): Promise<number> {
  const deployment = await createDeployment(request, projectId, 'scratch-dep', SAMPLE_A);
  const service = await createService(request, deployment, 'cache');
  return createEnvironment(request, service, name, content, '', fileType);
}

function row(page: Page, key: string) {
  return page.locator(`[data-fragment="kv-row"][data-key="${key}"]`);
}

function saveBar(page: Page) {
  return page.locator('#editor-save');
}

function saveButton(page: Page) {
  return saveBar(page).locator('button[type="submit"]');
}

function toast(page: Page) {
  return page.locator('[data-fragment="toast"]');
}

function header(page: Page) {
  return page.locator('[data-fragment="page-header"]');
}

async function reveal(page: Page, key: string): Promise<void> {
  await row(page, key).locator('button[hx-post*="/reveal/"]').click();
  await expect(row(page, key)).toHaveAttribute('data-revealed', 'true');
}

async function openEdit(page: Page, key: string): Promise<void> {
  await row(page, key).locator(`button[hx-get*="/rows/${key}/edit"]`).click();
  await expect(row(page, key).locator('textarea[name="edit_value"]')).toBeVisible();
}

test.describe('superadmin editor', () => {
  test('shows the masked variables with no value anywhere in the page', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.masked', CONTENT);
    await page.goto(`/environments/${id}`);
    await expectItemHeader(page, 'environment', '.env.masked');
    await expect(header(page).locator('[data-status]')).toHaveAttribute('data-status', 'never');
    await expectLocation(page, 'environment', {
      parents: `Project: ${scratch.name}, Deployment: scratch-dep, Service: cache`,
      root: `Project path: ${SAMPLE_A}`,
      target: `Target: ${SAMPLE_A}/environments/.env.masked`,
    });
    await expect(header(page)).toContainText('Version 1 saved, never deployed');
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
      'Edit',
      'History',
      'Compare to dist',
      'AccessSuperadmin',
    ]);
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'Edit',
    );
    await expect(page.locator('nav[aria-label="Breadcrumb"] li').last()).toHaveText('.env.masked');
    await expect(page.locator('[data-fragment="kv-row"]')).toHaveCount(3);
    await expect(row(page, 'PASSWORD')).toContainText('••••••••••••');
    await expect(row(page, 'PASSWORD').locator('button[data-js="reveal-hide"]')).toBeHidden();
    await expect(row(page, 'PASSWORD').locator('button[hx-post*="/reveal/"]')).toBeVisible();
    await expect(row(page, 'CERT')).toContainText('multiline value, 3 lines');
    await expect(page.locator('[data-fragment="editor-panel"] [role="group"] button')).toHaveText([
      'Key / value',
      'Raw text',
    ]);
    await expect(page.locator('[data-fragment="file-sidepanel"]')).toContainText(
      'environments/.env.masked',
    );
    await expect(page.locator('[data-fragment="file-sidepanel"]')).toContainText('Never deployed');
    await expectNoValueInPage(page, SECRET, 'db.example.test', CERTIFICATE_BODY, 'Connection');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('reveals one value, hides it again and drops it from the page', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.reveal', CONTENT);
    await page.goto(`/environments/${id}`);
    await reveal(page, 'PASSWORD');
    const revealed = row(page, 'PASSWORD').locator('[data-reveal-value]');
    await expect(revealed).toHaveText(SECRET);
    await expect(revealed).toHaveAttribute('hx-ignore', '');
    await expect(row(page, 'PASSWORD')).toContainText('Revealed and logged to the audit trail');
    await expect(row(page, 'HOST').locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, 'db.example.test');
    await row(page, 'PASSWORD').locator('button[data-js="reveal-hide"]').click();
    await expect(row(page, 'PASSWORD')).toHaveAttribute('data-revealed', 'false');
    await expect(row(page, 'PASSWORD').locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, SECRET);
    await reveal(page, 'CERT');
    const certificate = await row(page, 'CERT').locator('[data-reveal-value]').textContent();
    expect(certificate).toBe(
      `-----BEGIN CERTIFICATE-----\n${CERTIFICATE_BODY}\n-----END CERTIFICATE-----`,
    );
  });

  test('hides a revealed value by itself after 30 seconds', async ({ page, request, scratch }) => {
    const id = await prepare(request, scratch.id, '.env.timer', CONTENT);
    await page.clock.install();
    await page.goto(`/environments/${id}`);
    await reveal(page, 'PASSWORD');
    await expect(row(page, 'PASSWORD').locator('[data-reveal-value]')).toHaveText(SECRET);
    await page.clock.fastForward(29_000);
    await expect(row(page, 'PASSWORD')).toHaveAttribute('data-revealed', 'true');
    await page.clock.fastForward(2_000);
    await expect(row(page, 'PASSWORD')).toHaveAttribute('data-revealed', 'false');
    await expect(row(page, 'PASSWORD').locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, SECRET);
  });

  test('replaces a value without ever seeing the old one and saves a version', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.edit', CONTENT);
    await page.goto(`/environments/${id}`);
    await openEdit(page, 'HOST');
    await expect(row(page, 'HOST').locator('textarea[name="edit_value"]')).toHaveValue('');
    await expect(row(page, 'HOST')).toContainText('Edited');
    await expect(row(page, 'HOST')).toContainText('Leave blank to keep the current value.');
    await expectNoValueInPage(page, 'db.example.test');
    await row(page, 'HOST').locator('textarea[name="edit_value"]').fill('db2.example.test');
    await saveBar(page).locator('input[name="comment"]').fill('Move the database');
    await saveButton(page).click();
    await expect(page).toHaveURL(new RegExp(`/environments/${id}\\?saved=2$`));
    await expect(toast(page)).toContainText('Saved as version 2. Pending deploy.');
    await expect(header(page)).toContainText('Version 2 saved, never deployed');
    await expect(page.locator('[data-fragment="file-sidepanel"]')).toContainText('v2 · ');
    await reveal(page, 'HOST');
    await expect(row(page, 'HOST').locator('[data-reveal-value]')).toHaveText('db2.example.test');
    await reveal(page, 'PASSWORD');
    await expect(row(page, 'PASSWORD').locator('[data-reveal-value]')).toHaveText(SECRET);
  });

  test('cancel and discard changes bring the masked rows back', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.discard', CONTENT);
    await page.goto(`/environments/${id}`);
    await openEdit(page, 'HOST');
    await row(page, 'HOST').locator('button[hx-get*="/rows/HOST?"]').click();
    await expect(row(page, 'HOST').locator('textarea')).toHaveCount(0);
    await expect(row(page, 'HOST').locator('button[hx-post*="/reveal/"]')).toBeVisible();
    await openEdit(page, 'PASSWORD');
    await row(page, 'PASSWORD').locator('textarea[name="edit_value"]').fill('typed');
    await saveBar(page).getByRole('button').first().click();
    await expect(page.locator('[data-editing]')).toHaveCount(0);
    await expect(page.locator('textarea')).toHaveCount(0);
    await expect(page.locator('[data-fragment="kv-row"]')).toHaveCount(3);
  });

  test('adds and removes variables and a multiline value is stored as given', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.add', CONTENT);
    await page.goto(`/environments/${id}`);
    const addButton = page.locator('button[hx-get$="/new-row"]');
    await addButton.click();
    await addButton.click();
    await expect(page.locator('[data-new]')).toHaveCount(2);
    await page.locator('[data-new]').last().locator('[data-js="row-remove"]').click();
    await expect(page.locator('[data-new]')).toHaveCount(1);
    await page.locator('input[name="new_key"]').fill('ADDED');
    await page.locator('textarea[name="new_value"]').fill('line one\nline two');
    await openEdit(page, 'PASSWORD');
    await row(page, 'PASSWORD').locator('input[name="remove_key"]').check();
    await saveButton(page).click();
    await expect(page).toHaveURL(new RegExp(`\\?saved=2$`));
    await expect(row(page, 'PASSWORD')).toHaveCount(0);
    await expect(row(page, 'ADDED')).toContainText('multiline value, 2 lines');
    await reveal(page, 'ADDED');
    expect(await row(page, 'ADDED').locator('[data-reveal-value]').textContent()).toBe(
      'line one\nline two',
    );
    await expect(row(page, 'HOST')).toBeVisible();
  });

  test('refuses an unchanged save and a duplicate name and keeps what was typed', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.refuse', CONTENT);
    await page.goto(`/environments/${id}`);
    await saveButton(page).click();
    await expect(saveBar(page).locator('[role="alert"]')).toHaveText('No changes to save.');
    await expect(page).toHaveURL(new RegExp(`/environments/${id}$`));
    await page.locator('button[hx-get$="/new-row"]').click();
    await page.locator('input[name="new_key"]').fill('HOST');
    await page.locator('textarea[name="new_value"]').fill('kept text');
    await saveButton(page).click();
    await expect(saveBar(page).locator('[role="alert"]')).toHaveText('HOST already exists.');
    await expect(page.locator('input[name="new_key"]')).toHaveValue('HOST');
    await expect(page.locator('textarea[name="new_value"]')).toHaveValue('kept text');
    await page.locator('input[name="new_key"]').fill('NOT VALID');
    await saveButton(page).click();
    await expect(saveBar(page).locator('[role="alert"]')).toContainText('Use letters, numbers');
  });

  test('refuses a save from a page that is out of date', async ({ page, request, scratch }) => {
    const id = await prepare(request, scratch.id, '.env.stale', CONTENT);
    await page.goto(`/environments/${id}`);
    await saveRaw(request, id, 1, `${CONTENT}OTHER=1\n`);
    await openEdit(page, 'HOST');
    await row(page, 'HOST').locator('textarea[name="edit_value"]').fill('lost');
    await saveButton(page).click();
    await expect(saveBar(page).locator('[role="alert"]')).toContainText(
      'This file changed since you opened it.',
    );
    await expect(page).toHaveURL(new RegExp(`/environments/${id}$`));
    await expect(row(page, 'HOST').locator('textarea[name="edit_value"]')).toHaveValue('lost');
  });

  test('raw text is masked until an audited reveal and saves like key/value', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.raw', CONTENT);
    await page.goto(`/environments/${id}`);
    await page.locator('[role="group"] button[hx-get*="mode=raw"]').click();
    const pressed = page.locator('[role="group"] button[aria-pressed="true"]');
    await expect(pressed).toHaveAttribute('hx-get', /mode=raw/);
    await expect(page.locator('[data-fragment="raw-text"]')).toContainText('PASSWORD=••••••••••••');
    await expect(page.locator('textarea')).toHaveCount(0);
    await expectNoValueInPage(page, SECRET, 'Connection', 'db.example.test');
    await page.locator('button[hx-post*="/reveal-raw"]').click();
    const text = page.locator('textarea[name="content"]');
    await expect(text).toBeVisible();
    await expect(text).toHaveValue(CONTENT);
    await text.fill(`${CONTENT}RAW_ADDED=yes\r\n`);
    await saveBar(page).locator('input[name="comment"]').fill('via raw');
    await saveButton(page).click();
    await expect(page).toHaveURL(new RegExp(`/environments/${id}\\?saved=2$`));
    await expect(row(page, 'RAW_ADDED')).toBeVisible();
    await reveal(page, 'RAW_ADDED');
    await expect(row(page, 'RAW_ADDED').locator('[data-reveal-value]')).toHaveText('yes');
  });

  test('raw text hides after 30 seconds unless it was changed', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, '.env.rawtimer', CONTENT);
    await page.clock.install();
    await page.goto(`/environments/${id}?mode=raw`);
    await page.locator('button[hx-post*="/reveal-raw"]').click();
    await expect(page.locator('textarea[name="content"]')).toBeVisible();
    await page.clock.fastForward(31_000);
    await expect(page.locator('textarea')).toHaveCount(0);
    await expectNoValueInPage(page, SECRET);
    await page.locator('button[hx-post*="/reveal-raw"]').click();
    await page.locator('textarea[name="content"]').fill(`${CONTENT}TYPED=1\n`);
    await page.clock.fastForward(31_000);
    await expect(page.locator('textarea[name="content"]')).toHaveValue(`${CONTENT}TYPED=1\n`);
  });

  test('a certificate is edited as raw text only', async ({ page, request, scratch }) => {
    const id = await prepare(request, scratch.id, 'e2e-ca.pem', PEM, 'cert');
    await page.goto(`/environments/${id}`);
    await expect(page.locator('[role="group"][aria-label]')).toHaveCount(0);
    await expect(page.locator('[data-fragment="raw-text"]')).toContainText('3 lines');
    await expect(page.locator('[data-fragment="raw-text"]')).toContainText(
      'This is a certificate, so it is shown and edited as one text.',
    );
    await expect(page.locator('[data-fragment="file-sidepanel"]')).toContainText('Certificate');
    await expect(page.locator('[data-fragment="file-sidepanel"]')).toContainText('Not applicable');
    await expect(page.locator('input[name="mode"]')).toHaveValue('raw');
    await expectNoValueInPage(page, CERTIFICATE_BODY);
    await page.goto(`/environments/${id}?mode=kv`);
    await expect(page.locator('input[name="mode"]')).toHaveValue('raw');
    await expect(page.locator('#kv-list')).toHaveCount(0);
    await page.locator('button[hx-post*="/reveal-raw"]').click();
    await expect(page.locator('textarea[name="content"]')).toHaveValue(PEM);
    await page.locator('textarea[name="content"]').fill(`${PEM}# renewed\n`);
    await saveButton(page).click();
    await expect(page).toHaveURL(/\?saved=2$/);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a certificate save without a PEM block is refused and keeps the typed text', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, 'e2e-bad.pem', PEM, 'cert');
    await page.goto(`/environments/${id}`);
    await page.locator('button[hx-post*="/reveal-raw"]').click();
    await page.locator('textarea[name="content"]').fill('KEY=value\n');
    await saveButton(page).click();
    await expect(page.locator('#editor-save-error')).toContainText(
      'A certificate must hold at least one PEM block',
    );
    await expect(page.locator('textarea[name="content"]')).toHaveValue('KEY=value\n');
    await expect(page).toHaveURL(new RegExp(`/environments/${id}$`));
  });

  test('free text in an ENV file is edited as raw text with its own note', async ({
    page,
    request,
    scratch,
  }) => {
    const id = await prepare(request, scratch.id, 'e2e-free.txt', PEM);
    await page.goto(`/environments/${id}`);
    await expect(page.locator('[data-fragment="raw-text"]')).toContainText(
      'not in KEY=VALUE format',
    );
    await expect(page.locator('[data-fragment="file-sidepanel"]')).toContainText('ENV file');
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('sees masked values only and has no reveal, edit or save', async ({ page, browser }) => {
    const id = await seededEditor(browser);
    await page.goto(`/environments/${id}`);
    await expectItemHeader(page, 'environment', '.env.db.app');
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText(['View', 'History']);
    await expect(page.locator('main [role="note"]')).toContainText('Read-only view');
    await expect(page.locator('[data-fragment="kv-row"]')).toHaveCount(6);
    await expect(page.locator('[data-fragment="editor-panel"]')).toContainText('Values are masked.');
    await expect(page.locator('button[hx-post]')).toHaveCount(0);
    await expect(page.locator('[data-fragment="kv-row"] button')).toHaveCount(0);
    await expect(page.locator('#editor-save')).toHaveCount(0);
    await expect(page.locator('textarea, input[name="comment"]')).toHaveCount(0);
    await expectNoValueInPage(page, ...SEEDED_SECRETS, 'db.internal.example.com');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('raw mode is masked and offers no reveal', async ({ page, browser }) => {
    const id = await seededEditor(browser);
    await page.goto(`/environments/${id}`);
    await page.locator('[role="group"] button[hx-get*="mode=raw"]').click();
    await expect(page.locator('[data-fragment="raw-text"]')).toContainText(
      'MYSQL_PASSWORD=••••••••••••',
    );
    await expect(page.locator('button[hx-post]')).toHaveCount(0);
    await expectNoValueInPage(page, ...SEEDED_SECRETS, 'rotated by hand');
  });

  test('the server refuses reveal and save requests', async ({ page, browser }) => {
    const id = await seededEditor(browser);
    await page.goto(`/environments/${id}`);
    const headers = await csrfHeaders(page.request);
    const revealed = await page.request.post(`/environments/${id}/reveal/MYSQL_PASSWORD`, {
      headers,
    });
    expect(revealed.status()).toBe(403);
    expect(await revealed.text()).not.toContain(SEEDED_SECRETS[2]);
    const raw = await page.request.post(`/environments/${id}/reveal-raw`, { headers });
    expect(raw.status()).toBe(403);
    const saved = await page.request.post(`/environments/${id}/versions`, {
      form: { mode: 'raw', base_version: '3', content: 'A=1' },
      headers,
    });
    expect(saved.status()).toBe(403);
  });
});

test.describe('other roles', () => {
  test('an admin gets the controls and stays masked', async ({ browser }) => {
    const id = await seededEditor(browser);
    const context = await browser.newContext({ storageState: stateFile('admin') });
    const page = await context.newPage();
    try {
      await page.goto(`/environments/${id}`);
      await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
        'Edit',
        'History',
        'Compare to dist',
      ]);
      await expect(page.locator('button[hx-post*="/reveal/"]')).toHaveCount(6);
      await expect(page.locator('#editor-save')).toBeVisible();
      await expectNoValueInPage(page, ...SEEDED_SECRETS, 'db.internal.example.com');
    } finally {
      await context.close();
    }
  });

  test('an environment scoped admin reaches its file only', async ({ browser }) => {
    const owned = await seededDb(browser);
    const other = await seededEditor(browser);
    const context = await browser.newContext({ storageState: stateFile('envScopedAdmin') });
    const page = await context.newPage();
    try {
      const allowed = await page.goto(`/environments/${owned}`);
      expect(allowed?.status()).toBe(200);
      await expect(page.locator('[data-fragment="kv-row"]').first()).toBeVisible();
      const denied = await page.goto(`/environments/${other}`);
      expect(denied?.status()).toBe(404);
    } finally {
      await context.close();
    }
  });

  test('an admin without grants and a scoped viewer get 404 for the editor file', async ({
    browser,
  }) => {
    const other = await seededEditor(browser);
    await asRolesWithoutAccess(browser, async (page) => {
      expect((await page.goto(`/environments/${other}`))?.status()).toBe(404);
      const reveal = await page.request.post(`/environments/${other}/reveal/MYSQL_PASSWORD`, {
        headers: await csrfHeaders(page.request),
      });
      expect(reveal.status()).toBe(404);
    });
  });
});

async function seededDb(browser: Browser): Promise<number> {
  return seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
}
