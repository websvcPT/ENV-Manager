import { existsSync } from 'node:fs';

import { type Locator, type Page } from '@playwright/test';

import { stateFile } from '../global-setup';
import { createHostFolder, type HostFolder } from '../host-files';
import { listProjects } from '../projects-api';
import { serviceId } from '../services-api';
import { addAllowedPath, listAllowedPaths, removeAllowedPath } from '../settings-api';
import { test } from '../settings-test';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';

const TABS = [
  ['paths', 'Allowed paths'],
  ['smtp', 'SMTP'],
  ['git', 'Git'],
  ['backup', 'Backup'],
  ['security', 'Security'],
  ['post-deploy', 'Post-deploy'],
  ['notifications', 'Notifications'],
  ['general', 'General'],
] as const;
const DEFAULT_TIMEOUTS = { idle: '30', absolute: '12' };

/*
 * The Settings screens on the development stack. The stack has no Docker socket, so the
 * Post-deploy tab shows its disabled state here; the enabled state and the switch are covered
 * against the real daemon by the specs in docker-tests. A spec that changes a stored setting
 * holds a lock, changes only values no other spec reads (the session timeouts, the default
 * owner and group of a new deployment) and puts the defaults back.
 */
const { expect } = test;

async function height(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box?.height ?? 0;
}

function sidebarLink(page: Page): Locator {
  return page.locator('nav[aria-label="Primary"] a[href="/settings"]');
}

function tabLinks(page: Page): Locator {
  return page.locator('nav[aria-label="Sections"] a');
}

function currentTab(page: Page): Locator {
  return page.locator('nav[aria-label="Sections"] a[aria-current="page"]');
}

function toast(scope: Locator): Locator {
  return scope.locator('[data-fragment="toast"]');
}

function pathRow(page: Page, path: string): Locator {
  return page.locator('#paths-table tr[data-fragment="path-row"]', { hasText: path });
}

test.describe('layout and tabs', () => {
  test('the sidebar opens the first tab with the header and all eight tabs', async ({ page }) => {
    await page.goto('/projects');
    if (isWide(page)) {
      await sidebarLink(page).click();
    } else {
      await page.locator('[data-js="menu-open"]').click();
      await page.locator('#mobile-menu a[href="/settings"]').click();
    }
    await expect(page).toHaveURL(/\/settings\/paths$/);
    const header = page.locator('[data-fragment="page-header"]');
    await expect(header.locator('h1')).toHaveText('Settings');
    await expect(header).toContainText('Superadmin');
    await expect(header).toContainText('Application settings. Superadmin only.');
    await expect(tabLinks(page)).toHaveText(TABS.map(([, label]) => label));
    for (const [index, [slug]] of TABS.entries()) {
      await expect(tabLinks(page).nth(index)).toHaveAttribute('href', `/settings/${slug}`);
    }
    await expect(currentTab(page)).toHaveText('Allowed paths');
    await expect(page).toHaveTitle('Settings · Allowed paths · ENV Manager');
    if (isWide(page)) {
      const crumbs = page.locator('nav[aria-label="Breadcrumb"]');
      await expect(crumbs).toContainText('Settings');
      await expect(crumbs).toContainText('Allowed paths');
      await expect(sidebarLink(page)).toHaveAttribute('aria-current', 'page');
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  for (const [slug, label] of TABS) {
    test(`the ${label} tab opens with its panel and marks itself`, async ({ page }) => {
      await page.goto('/settings/paths');
      await tabLinks(page).filter({ hasText: label }).click();
      await expect(page).toHaveURL(new RegExp(`/settings/${slug}$`));
      await expect(page).toHaveTitle(`Settings · ${label} · ENV Manager`);
      await expect(currentTab(page)).toHaveText(label);
      await expect(page.locator('main section[aria-labelledby]').first()).toBeVisible();
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    });
  }

  test('the settings address answers with the first tab', async ({ page }) => {
    await page.goto('/settings');
    await expect(page).toHaveURL(/\/settings\/paths$/);
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

    test('has no settings link and is refused every settings address', async ({ page }) => {
      await page.goto('/projects');
      await expect(page.locator('a[href="/settings"]')).toHaveCount(0);
      for (const path of ['/settings', ...TABS.map(([slug]) => `/settings/${slug}`)]) {
        const response = await page.goto(path);
        expect(response?.status(), path).toBe(403);
        await expect(page.locator('main')).toContainText('No access');
      }
      for (const path of ['/settings/paths/table', '/settings/paths/new']) {
        const response = await page.request.get(path, { headers: { 'HX-Request': 'true' } });
        expect(response.status(), path).toBe(403);
      }
    });
  });
}

test.describe('allowed paths', () => {
  let folder: HostFolder;

  async function removeScratchEntry(page: Page): Promise<void> {
    for (const [id, path] of await listAllowedPaths(page.request)) {
      if (path === folder.root) {
        await removeAllowedPath(page.request, id);
      }
    }
  }

  test.beforeEach(() => {
    folder = createHostFolder();
  });

  test.afterEach(async ({ page }) => {
    await removeScratchEntry(page);
    folder.remove();
  });

  test('lists the entries with their status and the deployments under them', async ({ page }) => {
    await page.goto('/settings/paths');
    const panel = page.locator('#paths-table');
    await expect(panel).toContainText('The app may only read and write below these paths.');
    await expect(panel.locator('thead th')).toHaveText([
      'Path',
      'Mounted',
      'Writable',
      'Deployments',
      'Actions',
    ]);
    const mounted = pathRow(page, '/srv/deployments').first();
    await expect(mounted.locator('[data-status="deployed"]')).toHaveText(['Mounted', 'Writable']);
    await expect(mounted.locator('td').nth(2)).toHaveText(/^\d+$/);
    const missing = pathRow(page, '/opt/rentals');
    await expect(missing).toContainText('The folder no longer exists.');
    await expect(missing.locator('[data-status="failed"]')).toHaveText('Missing');
    await expect(missing.locator('[data-status="never"]')).toHaveText('Not checked');
    await expect(missing.locator('td').nth(2)).toHaveText('2');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the remove dialog counts the deployments that would be left outside', async ({ page }) => {
    await page.goto('/settings/paths');
    await pathRow(page, '/opt/rentals').locator('[data-row-action="remove"]').click();
    const dialog = page.locator('#path-remove');
    await expect(dialog.locator('h2')).toHaveText('Remove /opt/rentals');
    await expect(dialog).toContainText(
      'The 2 deployments under this path are flagged as outside allowed paths and cannot be deployed or checked until the path is allowed again or the deployments are moved.',
    );
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(pathRow(page, '/opt/rentals')).toBeVisible();
  });

  test('the add dialog checks the path as you type and adds it', async ({ page }) => {
    await page.goto('/settings/paths');
    await page.locator('[data-target="path-add"]').first().click();
    const dialog = page.locator('#path-add');
    await expect(dialog.locator('h2')).toHaveText('Add allowed path');
    await expect(dialog.locator('#ap')).toHaveValue('');
    await expect(dialog.locator('#ap-v')).toContainText('It is checked as you type.');
    const submit = dialog.locator('#path-add-submit');
    await expect(submit).toBeDisabled();

    await dialog.locator('#ap').fill('/');
    await expect(dialog.locator('#ap-v li')).toHaveText(
      'The filesystem root cannot be an allowed path.',
    );
    await expect(submit).toBeDisabled();

    await dialog.locator('#ap').fill(`${folder.root}/missing`);
    await expect(dialog.locator('#ap-v li')).toHaveText('Path does not exist.');
    await expect(submit).toBeDisabled();

    await dialog.locator('#ap').fill(folder.root);
    await expect(dialog.locator('#ap-v li')).toHaveCount(3);
    await expect(dialog.locator('#ap-v li').first()).toHaveText('Absolute and normalized');
    await expect(dialog.locator('#ap-v li').nth(1)).toHaveText('Exists');
    await expect(dialog.locator('#ap-v li').nth(2)).toContainText(
      'Inside the mount /srv/deployments, writable',
    );
    await expect(submit).toBeEnabled();
    if (!isWide(page)) {
      expect(await height(submit)).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }

    await submit.click();
    await expect(dialog).not.toHaveAttribute('open', '');
    const row = pathRow(page, folder.root);
    await expect(row).toBeVisible();
    await expect(row.locator('[data-status="deployed"]')).toHaveText(['Mounted', 'Writable']);
    await expect(row.locator('td').nth(2)).toHaveText('0');
  });

  test('a refused path shows the reason under the field and keeps Add disabled', async ({
    page,
  }) => {
    await page.goto('/settings/paths');
    await page.locator('[data-target="path-add"]').first().click();
    const dialog = page.locator('#path-add');
    await dialog.locator('#ap').fill('/');
    await expect(dialog.locator('#ap-v li')).toHaveCount(1);
    // A stale page or a script could still post it: the server refuses with the reason.
    const submit = dialog.locator('#path-add-submit');
    await submit.evaluate((button) => button.removeAttribute('disabled'));
    await submit.click();
    await expect(dialog.locator('#ap-e')).toHaveText(
      'The filesystem root cannot be an allowed path.',
    );
    await expect(dialog.locator('#ap')).toHaveValue('/');
    await expect(dialog.locator('#ap')).toHaveAttribute('aria-invalid', 'true');
    await expect(dialog.locator('#ap-v li')).toHaveText(
      'The filesystem root cannot be an allowed path.',
    );
    await expect(dialog.locator('#path-add-submit')).toBeDisabled();
    await expect(dialog).toHaveAttribute('open', '');
  });

  test('a path that is already in the list is refused', async ({ page }) => {
    await addAllowedPath(page.request, folder.root);
    await page.goto('/settings/paths');
    await page.locator('[data-target="path-add"]').first().click();
    const dialog = page.locator('#path-add');
    await dialog.locator('#ap').fill(folder.root);
    await expect(dialog.locator('#ap-v li')).toHaveText('This path is already in the list.');
    await expect(dialog.locator('#path-add-submit')).toBeDisabled();
  });

  test('removing a path asks first, deletes nothing on disk and refreshes the table', async ({
    page,
  }) => {
    folder.write('environments/.env.keep', 'KEEP=1\n');
    await addAllowedPath(page.request, folder.root);
    await page.goto('/settings/paths');
    const row = pathRow(page, folder.root);
    await expect(row).toBeVisible();
    const remove = row.locator('[data-row-action="remove"]');
    if (!isWide(page)) {
      expect(await height(remove)).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }
    await remove.click();
    const dialog = page.locator('#path-remove');
    await expect(dialog.locator('h2')).toHaveText(`Remove ${folder.root}`);
    await expect(dialog).toContainText(
      'Nothing is deleted. No deployment is left outside the allowed paths.',
    );
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(row).toBeVisible();

    await remove.click();
    await dialog.getByRole('button', { name: 'Remove path' }).click();
    await expect(dialog).not.toHaveAttribute('open', '');
    await expect(row).toHaveCount(0);
    expect(existsSync(folder.file('environments/.env.keep'))).toBe(true);
  });

  test('the dialog of a path that was removed meanwhile says so', async ({ page }) => {
    await addAllowedPath(page.request, folder.root);
    await page.goto('/settings/paths');
    const row = pathRow(page, folder.root);
    await row.locator('[data-row-action="remove"]').click();
    const dialog = page.locator('#path-remove');
    await expect(dialog.getByRole('button', { name: 'Remove path' })).toBeVisible();
    await removeScratchEntry(page);
    await dialog.getByRole('button', { name: 'Remove path' }).click();
    await expect(dialog).toContainText('This path is no longer in the list.');
    await expect(dialog.locator('button', { hasText: /^Close$/ })).toBeVisible();
    await expect(row).toHaveCount(0);
  });

  test('adding and removing are audited with the path', async ({ page }) => {
    await addAllowedPath(page.request, folder.root);
    await removeScratchEntry(page);
    for (const [action, detail] of [
      ['allowed_path.add', 'writable'],
      ['allowed_path.remove', 'deployments outside'],
    ] as const) {
      await page.goto(`/audit?action=${action}`);
      const row = page
        .locator('#audit-results tr[data-fragment="audit-row"]')
        .filter({ hasText: folder.root })
        .first();
      await expect(row).toContainText(action);
      await expect(row.locator('xpath=following-sibling::tr[1]')).toContainText(detail);
    }
  });
});

test.describe('security', () => {
  async function saveTimeouts(page: Page, idle: string, absolute: string): Promise<void> {
    await page.locator('#si').fill(idle);
    await page.locator('#sa').fill(absolute);
    await page.locator('#security button[type="submit"]').click();
  }

  async function restoreTimeouts(page: Page): Promise<void> {
    await page.goto('/settings/security');
    await saveTimeouts(page, DEFAULT_TIMEOUTS.idle, DEFAULT_TIMEOUTS.absolute);
    await expect(toast(page.locator('#security'))).toBeVisible();
    await expect(page.locator('#si')).toHaveValue(DEFAULT_TIMEOUTS.idle);
    await expect(page.locator('#sa')).toHaveValue(DEFAULT_TIMEOUTS.absolute);
  }

  test('shows the stored values, the fixed address limit and the two-factor policy read-only', async ({
    page,
  }) => {
    await page.goto('/settings/security');
    const form = page.locator('#security');
    for (const [id, label] of [
      ['si', 'Idle session timeout (minutes)'],
      ['sa', 'Absolute session timeout (hours)'],
      ['sl', 'Failed logins per user before lockout'],
      ['sg', 'Failed logins per address before lockout'],
      ['sk', 'Failed API attempts per address before lockout'],
      ['sm', 'Lockout duration (minutes)'],
      ['sp', 'Minimum password length'],
    ]) {
      await expect(form.locator(`label[for="${id}"]`)).toHaveText(label);
    }
    await expect(page.locator('#sl')).toHaveValue('5');
    await expect(page.locator('#sk')).toHaveValue('10');
    await expect(page.locator('#sm')).toHaveValue('15');
    await expect(page.locator('#sp')).toHaveValue('12');
    // The development stack fixes the sign-in limit per address through the environment.
    await expect(page.locator('#sg')).toBeDisabled();
    await expect(page.locator('#sg')).toHaveValue(/^\d+$/);
    await expect(page.locator('#sg-h')).toContainText('ENVMANAGER_LOGIN_IP_MAX_FAILURES');
    const policy = form.locator('[data-two-factor-policy]');
    await expect(policy).toContainText('Two-factor authentication policy');
    await expect(policy).toContainText('Optional');
    await expect(policy).toContainText('Users can enable TOTP in their account.');
    await expect(form.locator('input[name="tfa"]')).toHaveCount(0);
    await expect(form.locator('input[type="radio"]')).toHaveCount(0);
    await expect(form).not.toContainText('decided policy');
    if (!isWide(page)) {
      expect(await height(form.locator('button[type="submit"]'))).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a refused value shows its reason next to the field and stores nothing', async ({
    page,
  }) => {
    await page.goto('/settings/security');
    await page.locator('#si').fill('soon');
    await page.locator('#sa').fill('0');
    await page.locator('#security button[type="submit"]').click();
    await expect(page.locator('#si-e')).toHaveText('Enter a whole number from 5 to 1440.');
    await expect(page.locator('#sa-e')).toHaveText('Enter a whole number from 1 to 168.');
    await expect(page.locator('#si')).toHaveValue('soon');
    await expect(page.locator('#si')).toHaveAttribute('aria-invalid', 'true');
    await expect(toast(page.locator('#security'))).toHaveCount(0);

    await page.locator('#si').fill('300');
    await page.locator('#sa').fill('2');
    await page.locator('#security button[type="submit"]').click();
    await expect(page.locator('#si-e')).toHaveText(
      'The idle timeout cannot be longer than the absolute timeout.',
    );
    await expect(page.locator('#sa-e')).toHaveCount(0);
  });

  test('saves the timeouts, says when nothing changed, and the defaults come back', async ({
    page,
    settingsLock: _lock,
  }, testInfo) => {
    const mine =
      testInfo.project.name === 'phone'
        ? { idle: '33', absolute: '14' }
        : { idle: '31', absolute: '13' };
    try {
      await page.goto('/settings/security');
      await saveTimeouts(page, mine.idle, mine.absolute);
      const saved = toast(page.locator('#security'));
      await expect(saved).toContainText('Security settings saved');
      await expect(saved).toContainText('They apply now.');
      await expect(page.locator('#si')).toHaveValue(mine.idle);
      await expect(page.locator('#sa')).toHaveValue(mine.absolute);

      await page.reload();
      await expect(page.locator('#si')).toHaveValue(mine.idle);
      await expect(page.locator('#sa')).toHaveValue(mine.absolute);

      await page.locator('#security button[type="submit"]').click();
      await expect(toast(page.locator('#security'))).toContainText('Nothing to save');
      await expect(toast(page.locator('#security'))).toContainText('The settings are unchanged.');
    } finally {
      await restoreTimeouts(page);
    }
    await page.goto('/settings/security');
    await expect(page.locator('#si')).toHaveValue(DEFAULT_TIMEOUTS.idle);
    await expect(page.locator('#sa')).toHaveValue(DEFAULT_TIMEOUTS.absolute);
  });

  test('the change is in the audit log with the old and the new value', async ({
    page,
    settingsLock: _lock,
  }, testInfo) => {
    const mine = testInfo.project.name === 'phone' ? '34' : '32';
    try {
      await page.goto('/settings/security');
      await page.locator('#si').fill(mine);
      await page.locator('#security button[type="submit"]').click();
      await expect(toast(page.locator('#security'))).toContainText('Security settings saved');
      await page.goto('/audit?action=settings.update');
      const first = page.locator('#audit-results tr[data-fragment="audit-row"]').first();
      await expect(first).toContainText('settings.update');
      const details = first.locator('xpath=following-sibling::tr[1]');
      await expect(details).toContainText('security.idle_timeout_minutes');
      await expect(details).toContainText(`old ${DEFAULT_TIMEOUTS.idle}`);
      await expect(details).toContainText(`new ${mine}`);
    } finally {
      await restoreTimeouts(page);
    }
  });
});

test.describe('post-deploy without a Docker socket', () => {
  test('shows the status, disables the switch and refuses a save', async ({ page }) => {
    await page.goto('/settings/post-deploy');
    const panel = page.locator('[data-fragment="settings-postdeploy"]');
    await expect(panel.locator('h2')).toHaveText('Post-deploy commands');
    await expect(panel.locator('[role="note"]')).toContainText(
      'Post-deploy commands are off because the Docker socket is not mounted.',
    );
    await expect(panel.locator('[role="note"]')).toContainText('/var/run/docker.sock');
    await expect(panel.locator('#docker-socket [data-status="failed"]')).toHaveText('Not mounted');
    await expect(panel.locator('#docker-daemon [data-status="never"]')).toHaveText('Not checked');
    await expect(panel.locator('#docker-compose')).toContainText('Unknown');
    const form = page.locator('#post-deploy-switch');
    await expect(form.locator('#pd-enabled')).toBeDisabled();
    await expect(form.locator('#pd-enabled')).not.toBeChecked();
    await expect(form).toContainText('Unavailable until the Docker socket is mounted.');
    await expect(form.locator('button[type="submit"]')).toBeDisabled();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    // A stale page or a script could still post it: the server refuses with the reason.
    await form.locator('#pd-enabled').evaluate((box) => box.removeAttribute('disabled'));
    await form
      .locator('button[type="submit"]')
      .evaluate((button) => button.removeAttribute('disabled'));
    await form.locator('#pd-enabled').check();
    await form.locator('button[type="submit"]').click();
    await expect(page.locator('#post-deploy-switch [role="alert"]')).toHaveText(
      'The Docker socket is not mounted. Post-deploy commands are off, so this setting cannot be changed.',
    );
    await expect(toast(page.locator('#post-deploy-switch'))).toHaveCount(0);
  });

  test('the link of the service page opens this tab', async ({ page, browser }) => {
    const id = await serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
    await page.goto(`/services/${id}`);
    await page
      .locator('#postdeploy [data-fragment="docker-status"]')
      .getByRole('link', { name: 'Open post-deploy settings' })
      .click();
    await expect(page).toHaveURL(/\/settings\/post-deploy$/);
    await expect(page.locator('[data-fragment="settings-postdeploy"]')).toBeVisible();
  });
});

test.describe('general', () => {
  test('shows the zone, the format, the defaults and the diagnostics', async ({ page }) => {
    await page.goto('/settings/general');
    const form = page.locator('#general');
    await expect(form.locator('#tz')).toHaveValue('UTC');
    await expect(form.locator('#tz option[value="Europe/Lisbon"]')).toHaveCount(1);
    await expect(form.locator('#df option')).toHaveText([
      '2026-10-17 09:42',
      '17/10/2026 09:42',
      '10/17/2026 9:42 AM',
    ]);
    await expect(form.locator('#d1')).toHaveValue('block');
    await expect(form.locator('#d2')).toHaveValue('halt');
    await expect(form.locator('#d3')).toHaveValue('create');
    await expect(form.locator('#d4')).toHaveValue('0600');
    await expect(form.locator('#d5')).toHaveAttribute('placeholder', 'app user');
    await expect(form).toContainText('Defaults for new deployments');

    const diagnostics = page.locator('[data-fragment="settings-diagnostics"]');
    await expect(diagnostics).toContainText('Read-only. Set at startup.');
    await expect(diagnostics.locator('dt')).toHaveText([
      'Sentry',
      'OpenTelemetry',
      'OTLP endpoint host',
      'Version',
      'Environment',
      'Trusted proxies',
      'Allowed sources',
      'Your address',
    ]);
    await expect(diagnostics.locator('#diag-version')).toHaveText(/^v\d+\.\d+\.\d+/);
    await expect(diagnostics.locator('#diag-environment')).toHaveText('development');
    await expect(diagnostics.locator('#diag-client')).toHaveText(/\d+\.\d+\.\d+\.\d+|:/);
    await expect(diagnostics.locator('#diag-sentry [data-status]')).toHaveText(/^(On|Off)$/);
    await expect(diagnostics.locator('#diag-otel [data-status]')).toHaveText(/^(On|Off)$/);
    const text = await page.locator('main').innerText();
    expect(text).not.toContain('ZGV2LW9ubHktZW52LW1hbmFnZXIta2V5LTAxMjM0NTY=');
    expect(text).not.toContain('dev-not-secret');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a refused value shows its reason next to the field', async ({ page }) => {
    await page.goto('/settings/general');
    await page.locator('#d4').fill('999');
    await page.locator('#d5').fill('bad name!');
    await page.locator('#general button[type="submit"]').click();
    await expect(page.locator('#d4-e')).toHaveText('Use an octal mode such as 0600.');
    await expect(page.locator('#d5-e')).toContainText('Owner must be a user or group name');
    await expect(page.locator('#d4')).toHaveValue('999');
    await expect(toast(page.locator('#general'))).toHaveCount(0);
  });

  test('saves the defaults, pre-fills the new deployment form and puts the defaults back', async ({
    page,
    settingsLock: _lock,
  }, testInfo) => {
    const owner = `e2e-owner-${testInfo.project.name}`;
    const [[fancy]] = await listProjects(page.request, 'Fancy Proj. Name');
    try {
      await page.goto('/settings/general');
      await page.locator('#d5').fill(owner);
      await page.locator('#d6').fill('e2e-group');
      await page.locator('#general button[type="submit"]').click();
      await expect(toast(page.locator('#general'))).toContainText('General settings saved');
      await expect(page.locator('#d5')).toHaveValue(owner);

      await page.goto(`/projects/${fancy}/deployments/new`);
      await expect(page.locator('#owner')).toHaveValue(owner);
      await expect(page.locator('#group')).toHaveValue('e2e-group');
      await expect(page.locator('#mode')).toHaveValue('0600');

      await page.goto('/settings/general');
      await expect(page.locator('#d6')).toHaveValue('e2e-group');
      await page.locator('#general button[type="submit"]').click();
      await expect(toast(page.locator('#general'))).toContainText('Nothing to save');
    } finally {
      await page.goto('/settings/general');
      await page.locator('#d5').fill('');
      await page.locator('#d6').fill('');
      await page.locator('#general button[type="submit"]').click();
      await expect(toast(page.locator('#general'))).toBeVisible();
      await expect(page.locator('#d5')).toHaveValue('');
    }
    await page.goto(`/projects/${fancy}/deployments/new`);
    await expect(page.locator('#owner')).toHaveValue('');
    await expect(page.locator('#group')).toHaveValue('');
  });
});
