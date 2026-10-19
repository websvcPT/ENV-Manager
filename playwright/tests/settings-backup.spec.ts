import { readFileSync, statSync } from 'node:fs';

import { type Locator, type Page } from '@playwright/test';

import { auditEntryIds } from '../audit-api';
import { stateFile } from '../global-setup';
import { createHostFolder } from '../host-files';
import { csrfHeaders } from '../projects-api';
import { test } from '../settings-test';

const { expect } = test;
const WIDE_BREAKPOINT = 768;
const TOUCH_TARGET = 44;
const SQLITE_HEADER = 'SQLite format 3\u0000';
const BACKUP_FILE = /^envmanager-\d{8}T\d{12}Z-manual\.db$/;

/*
 * The Backup tab on the development stack, which shares its database with every other spec: a
 * spec that saves a setting or takes a backup holds the settings lock (the desktop and phone
 * projects would otherwise refuse each other's backup) and puts the defaults back. Restore is
 * never confirmed here, because it would replace the database every other spec reads; the dialog
 * is opened and cancelled, and the restore itself is covered by the server tests.
 */

function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

async function height(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box?.height ?? 0;
}

function rows(page: Page): Locator {
  return page.locator('#backups tr[data-fragment="backup-row"]');
}

function form(page: Page): Locator {
  return page.locator('#backup');
}

function toast(scope: Locator): Locator {
  return scope.locator('[data-fragment="toast"]');
}

async function runNow(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Run now' }).click();
}

async function saveForm(page: Page): Promise<void> {
  await form(page).locator('button[type="submit"]').click();
}

/** Put the schedule, retention and extra destination back to the defaults. */
async function restoreDefaults(page: Page): Promise<void> {
  await page.goto('/settings/backup');
  await page.locator('#bs').selectOption('daily');
  await page.locator('#br').selectOption('count:14');
  await page.locator('#bx').fill('');
  await saveForm(page);
  await expect(toast(form(page))).toBeVisible();
}

test.describe('the tab', () => {
  test('shows the schedule form and the list of backups', async ({ page }) => {
    await page.goto('/settings/backup');
    await expect(page).toHaveTitle('Settings · Backup · ENV Manager');
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'Backup',
    );
    const settings = page.locator('[data-fragment="settings-backup"]');
    await expect(settings.locator('h2')).toHaveText('Schedule and retention');
    await expect(settings.locator('#bs option')).toHaveText([
      'Off',
      'Daily at 02:00',
      'Every 6 hours',
      'Weekly',
    ]);
    await expect(settings.locator('label[for="bs"]')).toHaveText('Schedule');
    await expect(settings.locator('label[for="br"]')).toHaveText('Retention');
    await expect(settings.locator('label[for="bx"]')).toHaveText('Extra destination (optional)');
    await expect(settings).toContainText('Must be inside an allowed path.');
    await expect(settings.getByRole('button', { name: 'Save settings' })).toBeVisible();
    const list = page.locator('#backups');
    await expect(list.locator('h2')).toHaveText('Backups');
    await expect(list.getByRole('button', { name: 'Run now' })).toBeVisible();
    await expect(page.locator('#backup-restore')).toBeHidden();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('keeps the touch targets and the table inside the page on a phone', async ({ page }) => {
    await page.goto('/settings/backup');
    if (isWide(page)) {
      return;
    }
    await expect(page.locator('#bs')).toBeVisible();
    expect(await height(form(page).locator('button[type="submit"]'))).toBeGreaterThanOrEqual(
      TOUCH_TARGET,
    );
    expect(await height(page.getByRole('button', { name: 'Run now' }))).toBeGreaterThanOrEqual(
      TOUCH_TARGET,
    );
    const first = rows(page).first();
    if ((await rows(page).count()) > 0 && (await first.locator('a').count()) > 0) {
      expect(await height(first.locator('a').first())).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect(await height(first.locator('button').first())).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('refuses an extra destination outside the allowed paths and a relative one', async ({
    page,
  }) => {
    await page.goto('/settings/backup');
    await page.locator('#bx').fill('/etc');
    await saveForm(page);
    await expect(page.locator('#bx-e')).toHaveText('Must be inside an allowed path.');
    await expect(page.locator('#bx')).toHaveValue('/etc');
    await expect(toast(form(page))).toHaveCount(0);
    await page.locator('#bx').fill('backups');
    await saveForm(page);
    await expect(page.locator('#bx-e')).toHaveText('Path must be absolute.');
    await page.reload();
    await expect(page.locator('#bx')).toHaveValue('');
  });
});

test.describe('settings', () => {
  test('saves the schedule and retention, says when nothing changed, and the defaults return', async ({
    page,
    settingsLock: _lock,
  }) => {
    try {
      await page.goto('/settings/backup');
      await page.locator('#bs').selectOption('weekly');
      await page.locator('#br').selectOption('days:30');
      await saveForm(page);
      const saved = toast(form(page));
      await expect(saved).toContainText('Backup settings saved');
      await expect(saved).toContainText('The schedule and retention apply now.');
      await page.reload();
      await expect(page.locator('#bs')).toHaveValue('weekly');
      await expect(page.locator('#br')).toHaveValue('days:30');
      await saveForm(page);
      await expect(toast(form(page))).toContainText('Nothing to save');
    } finally {
      await restoreDefaults(page);
    }
    await page.goto('/settings/backup');
    await expect(page.locator('#bs')).toHaveValue('daily');
    await expect(page.locator('#br')).toHaveValue('count:14');
  });
});

test.describe('backups', () => {
  test('Run now adds a manual backup first in the list with Download and Restore', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/backup');
    const before = await rows(page)
      .first()
      .getAttribute('id')
      .catch(() => null);
    await runNow(page);
    await expect(toast(page.locator('#backups'))).toContainText('Backup created');
    const first = rows(page).first();
    await expect(first).toContainText('Manual');
    await expect(first.locator('[data-status="success"]')).toHaveText('Success');
    expect(await first.getAttribute('id')).not.toBe(before);
    await expect(first.locator('td').nth(1)).toHaveText(/^\d+(\.\d)? (B|KB|MB|GB)$/);
    await expect(first.getByRole('link', { name: /Download/ })).toHaveAttribute(
      'href',
      /^\/settings\/backups\/\d+\/download$/,
    );
    await expect(first.getByRole('button', { name: /Restore/ })).toBeVisible();
    await expect(page.locator('[data-fragment="banner-area"]')).not.toContainText('Backup');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('Download streams the backup, not cached, and writes an audit entry', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/backup');
    if ((await rows(page).count()) === 0) {
      await runNow(page);
      await expect(rows(page).first()).toBeVisible();
    }
    const href = await rows(page).first().getByRole('link', { name: /Download/ }).getAttribute('href');
    expect(href).toMatch(/^\/settings\/backups\/\d+\/download$/);
    const before = await auditEntryIds(page.request, 'action=backup.download');
    const response = await page.request.get(href ?? '');
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toBe('application/vnd.sqlite3');
    expect(response.headers()['content-disposition']).toMatch(
      /^attachment; filename="envmanager-\d{8}T\d{12}Z-[a-z-]+\.db"$/,
    );
    expect(response.headers()['cache-control']).toBe('no-store');
    const body = await response.body();
    expect(body.subarray(0, SQLITE_HEADER.length).toString('latin1')).toBe(SQLITE_HEADER);
    expect(Number(response.headers()['content-length'])).toBe(body.length);
    expect(body.includes(Buffer.from('ZGV2LW9ubHktZW52LW1hbmFnZXIta2V5LTAxMjM0NTY='))).toBe(false);
    const after = await auditEntryIds(page.request, 'action=backup.download');
    expect(after.some((id) => !before.includes(id))).toBe(true);
  });

  test('copies the backup to an extra destination inside the allowed paths', async ({
    page,
    settingsLock: _lock,
  }) => {
    const host = createHostFolder();
    try {
      await page.goto('/settings/backup');
      await page.locator('#bx').fill(host.root);
      await saveForm(page);
      await expect(toast(form(page))).toContainText('Backup settings saved');
      await expect(page.locator('#bx')).toHaveValue(host.root);
      await runNow(page);
      await expect(toast(page.locator('#backups'))).toContainText('Backup created');
      await expect(toast(page.locator('#backups'))).not.toContainText('not copied');
      const copies = host.list().filter((name) => BACKUP_FILE.test(name));
      expect(copies).toHaveLength(1);
      const path = host.file(copies[0]);
      expect(statSync(path).mode & 0o777).toBe(0o600);
      expect(readFileSync(path).subarray(0, SQLITE_HEADER.length).toString('latin1')).toBe(
        SQLITE_HEADER,
      );
    } finally {
      await restoreDefaults(page);
      host.remove();
    }
  });

  test('a backup that cannot be copied says so and raises the banner until one succeeds', async ({
    page,
    settingsLock: _lock,
  }) => {
    const host = createHostFolder();
    try {
      await page.goto('/settings/backup');
      await page.locator('#bx').fill(host.root);
      await saveForm(page);
      await expect(toast(form(page))).toContainText('Backup settings saved');
      host.remove();
      await page.reload();
      await expect(page.locator('#bx-h')).toContainText('Backups are not copied there until this is fixed.');
      await runNow(page);
      const result = toast(page.locator('#backups'));
      await expect(result).toContainText('Backup created, not copied');
      await expect(result).toContainText('The extra destination folder does not exist.');
      const first = rows(page).first();
      await expect(first.locator('[data-status="partial"]')).toHaveText('Not copied');
      await expect(first.getByRole('link', { name: /Download/ })).toBeVisible();
      const banner = page.locator('[data-fragment="banner-area"]');
      await expect(banner).toContainText('Backup incomplete: the last backup (');
      await page.goto('/projects');
      await expect(page.locator('[data-fragment="banner-area"]')).toContainText(
        'could not be copied to the extra destination',
      );
      await page
        .locator('[data-fragment="banner-area"] a', { hasText: 'Open backup settings' })
        .click();
      await expect(page).toHaveURL(/\/settings\/backup$/);
    } finally {
      await restoreDefaults(page);
    }
    await runNow(page);
    await expect(toast(page.locator('#backups'))).toContainText('Backup created');
    await expect(page.locator('[data-fragment="banner-area"]')).not.toContainText('Backup');
    await page.goto('/projects');
    await expect(page.locator('[data-fragment="banner-area"]')).not.toContainText('Backup');
  });

  test('the restore dialog names the backup, warns, and Cancel restores nothing', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/backup');
    if ((await rows(page).count()) === 0) {
      await runNow(page);
      await expect(rows(page).first()).toBeVisible();
    }
    const first = rows(page).first();
    const when = (await first.locator('time').first().innerText()).trim();
    const before = await auditEntryIds(page.request, 'action=backup.restore');
    await first.getByRole('button', { name: /Restore/ }).click();
    const dialog = page.locator('#backup-restore');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('h2')).toHaveText(`Restore backup ${when}`);
    await expect(dialog).toContainText('Replaces all current data with this backup.');
    await expect(dialog).toContainText(`Changes made since ${when} are lost.`);
    await expect(dialog).toContainText('safety backup');
    await expect(dialog).toContainText('everyone is signed out');
    await expect(dialog.getByRole('button', { name: 'Restore backup' })).toBeVisible();
    if (isWide(page)) {
      expect((await dialog.boundingBox())?.width).toBeLessThanOrEqual(640);
    }
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();
    await page.goto('/projects');
    await expect(page).toHaveURL(/\/projects$/);
    expect(await auditEntryIds(page.request, 'action=backup.restore')).toEqual(before);
  });
});

for (const role of [
  'admin',
  'viewer',
  'deploymentAdmin',
  'envScopedAdmin',
  'envScopedViewer',
  'ungrantedAdmin',
] as const) {
  test.describe(`${role}`, () => {
    test.use({ storageState: stateFile(role) });

    test('cannot open, run, download or restore backups', async ({ page }) => {
      const response = await page.goto('/settings/backup');
      expect(response?.status()).toBe(403);
      await expect(page.locator('main')).toContainText('No access');
      for (const path of ['/settings/backups/1/download', '/settings/backups/1/restore']) {
        const answer = await page.request.get(path, { headers: { 'HX-Request': 'true' } });
        expect(answer.status(), path).toBe(403);
      }
      for (const path of ['/settings/backups', '/settings/backups/1/restore', '/settings/backup']) {
        const answer = await page.request.post(path, { headers: await csrfHeaders(page.request) });
        expect(answer.status(), path).toBe(403);
      }
    });
  });
}
