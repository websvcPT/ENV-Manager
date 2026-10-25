import { type Locator, type Page, type TestInfo } from '@playwright/test';

import { addRegistry, listRegistries, removeRegistry } from '../settings-api';
import { test as base } from '../settings-test';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';
import { actionControls, rowAction } from './row-actions';

const TOKEN = 'e2e-registry-token-sentinel';
const REPLACEMENT = 'e2e-registry-replacement-sentinel';

/*
 * The Registries tab on the development stack, which has no Docker socket, so Test login shows as
 * unavailable here with its reason on the panel; the test itself is covered against the real
 * daemon by the specs in docker-tests. The logins are shared application data, so every test
 * gets a host of its own (unique per project and run) and the fixture removes it afterwards.
 */

function scratchHost(testInfo: TestInfo): string {
  const run = `${Date.now().toString(36)}${testInfo.parallelIndex}${testInfo.repeatEachIndex}`;
  return `e2e-reg-${testInfo.project.name}-${run}.example.test`;
}

const test = base.extend<{ host: string }>({
  host: async ({ page }, use, testInfo) => {
    const host = scratchHost(testInfo);
    try {
      await use(host);
    } finally {
      for (const [id, listed] of await listRegistries(page.request)) {
        if (listed === host) {
          await removeRegistry(page.request, id);
        }
      }
    }
  },
});
const { expect } = test;

async function height(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box?.height ?? 0;
}

function registryRow(page: Page, host: string): Locator {
  return page.locator('#registries-table tr[data-fragment="registry-row"]', { hasText: host });
}

async function openAddDialog(page: Page): Promise<Locator> {
  await page.locator('[data-target="registry-add"]').first().click();
  const dialog = page.locator('#registry-add');
  await expect(dialog.locator('#ra-host')).toBeVisible();
  return dialog;
}

async function openEditDialog(page: Page, host: string): Promise<Locator> {
  await (await rowAction(registryRow(page, host), 'edit')).click();
  const dialog = page.locator('#registry-edit');
  await expect(dialog.locator('#re-host')).toBeVisible();
  return dialog;
}

async function auditDetails(page: Page, action: string, host: string): Promise<Locator> {
  await page.goto(`/audit?action=${action}`);
  const row = page
    .locator('#audit-results tr[data-fragment="audit-row"]')
    .filter({ hasText: host })
    .first();
  await expect(row).toContainText(action);
  return row.locator('xpath=following-sibling::tr[1]');
}

test('the tab explains the logins, lists them without the token and offers no test without Docker', async ({
  page,
  host,
}) => {
  await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  await expect(page).toHaveTitle('Settings · Registries · ENV Manager');
  const panel = page.locator('#registries-table');
  await expect(panel).toContainText('Post-deploy commands sign in to these registries');
  await expect(panel).toContainText('A pull-only bot account or a read-only token');
  await expect(panel.locator('thead th')).toHaveText([
    'Host',
    'Username',
    'Token',
    'Updated',
    'Actions',
  ]);
  const row = registryRow(page, host);
  await expect(row.locator('th')).toHaveText(host);
  await expect(row.locator('td').nth(0)).toHaveText('bot');
  await expect(row.locator('td').nth(1)).toHaveText('Stored');
  await expect(row.locator('td').nth(2).locator('time')).toBeVisible();
  await expect(page.locator('main')).not.toContainText(TOKEN);
  expect(await page.content()).not.toContain(TOKEN);

  await expect(actionControls(row, 'test')).toHaveCount(0);
  await expect(actionControls(row, 'edit')).toHaveCount(1);
  await expect(actionControls(row, 'remove')).toHaveCount(1);
  const note = page.locator('#registries-docker-note');
  await expect(note).toBeVisible();
  await expect(note).toContainText('The Docker socket is not mounted. Test login is unavailable.');
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
});

test('the add dialog opens empty with a password field and adds a normalized login', async ({
  page,
  host,
}) => {
  await page.goto('/settings/registries');
  const dialog = await openAddDialog(page);
  await expect(dialog.locator('h2')).toHaveText('Add registry login');
  await expect(dialog.locator('#ra-host')).toHaveValue('');
  await expect(dialog.locator('#ra-user')).toHaveValue('');
  await expect(dialog.locator('#ra-token')).toHaveValue('');
  await expect(dialog.locator('#ra-token')).toHaveAttribute('type', 'password');
  await expect(dialog.locator('#ra-token')).toHaveAttribute('autocomplete', 'new-password');

  await dialog.locator('#ra-host').fill(`https://${host.toUpperCase()}/`);
  await dialog.locator('#ra-user').fill('bot');
  await dialog.locator('#ra-token').fill(TOKEN);
  const submit = dialog.locator('button[type="submit"]');
  if (!isWide(page)) {
    expect(await height(submit)).toBeGreaterThanOrEqual(TOUCH_TARGET);
    expect(await height(dialog.locator('#ra-token'))).toBeGreaterThanOrEqual(TOUCH_TARGET);
  }
  await submit.click();
  await expect(dialog).not.toHaveAttribute('open', '');
  const row = registryRow(page, host);
  await expect(row).toBeVisible();
  await expect(row.locator('th')).toHaveText(host);
  await expect(row.locator('td').nth(0)).toHaveText('bot');
  await expect(page.locator('main')).not.toContainText(TOKEN);
  expect(await page.content()).not.toContain(TOKEN);
});

test('a refused host shows the reason, keeps what was typed except the token, and can be fixed', async ({
  page,
  host,
}) => {
  await page.goto('/settings/registries');
  const dialog = await openAddDialog(page);
  await dialog.locator('#ra-host').fill('not a host');
  await dialog.locator('#ra-user').fill('bot');
  await dialog.locator('#ra-token').fill(TOKEN);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog.locator('#ra-host-e')).toBeVisible();
  await expect(dialog.locator('#ra-host')).toHaveAttribute('aria-invalid', 'true');
  await expect(dialog.locator('#ra-host')).toHaveValue('not a host');
  await expect(dialog.locator('#ra-user')).toHaveValue('bot');
  await expect(dialog.locator('#ra-token')).toHaveValue('');
  await expect(dialog).toHaveAttribute('open', '');
  expect(await page.content()).not.toContain(TOKEN);

  await dialog.locator('#ra-host').fill(host);
  await dialog.locator('#ra-token').fill(TOKEN);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).not.toHaveAttribute('open', '');
  await expect(registryRow(page, host)).toBeVisible();
});

test('a host that is already in the list is refused and a missing token is asked for', async ({
  page,
  host,
}) => {
  await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  const dialog = await openAddDialog(page);
  await dialog.locator('#ra-host').fill(host.toUpperCase());
  await dialog.locator('#ra-user').fill('other');
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog.locator('#ra-token-e')).toBeVisible();
  await dialog.locator('#ra-token').fill(TOKEN);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog.locator('#ra-host-e')).toContainText('is already in the list.');
  await expect(dialog.locator('#ra-token')).toHaveValue('');
  await expect(registryRow(page, host)).toHaveCount(1);
});

test('an edit without a token keeps the stored one, and says so in the audit log', async ({
  page,
  host,
}) => {
  await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  const dialog = await openEditDialog(page, host);
  await expect(dialog.locator('h2')).toHaveText('Edit registry login');
  await expect(dialog.locator('#re-host')).toHaveValue(host);
  await expect(dialog.locator('#re-user')).toHaveValue('bot');
  await expect(dialog.locator('#re-token')).toHaveValue('');
  await expect(dialog.locator('#re-token')).toHaveAttribute('placeholder', '••••••••');
  await expect(dialog.locator('#re-token-h')).toHaveText('Stored. Leave empty to keep it.');
  expect(await page.content()).not.toContain(TOKEN);

  await dialog.locator('#re-user').fill('robot');
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).not.toHaveAttribute('open', '');
  await expect(registryRow(page, host).locator('td').nth(0)).toHaveText('robot');

  const details = await auditDetails(page, 'registry.update', host);
  await expect(details).toContainText('username changed: yes');
  await expect(details).toContainText('token changed: no');
  await expect(details).not.toContainText(TOKEN);
});

test('an edit with a token replaces it and the dialog never shows it back', async ({
  page,
  host,
}) => {
  await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  let dialog = await openEditDialog(page, host);
  await dialog.locator('#re-token').fill(REPLACEMENT);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).not.toHaveAttribute('open', '');
  expect(await page.content()).not.toContain(REPLACEMENT);

  dialog = await openEditDialog(page, host);
  await expect(dialog.locator('#re-token')).toHaveValue('');
  expect(await page.content()).not.toContain(REPLACEMENT);
  await dialog.getByRole('button', { name: 'Cancel' }).click();

  const details = await auditDetails(page, 'registry.update', host);
  await expect(details).toContainText('token changed: yes');
  await expect(details).not.toContainText(REPLACEMENT);
});

test('a refused edit keeps the typed username and shows the reason', async ({ page, host }) => {
  await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  const dialog = await openEditDialog(page, host);
  await dialog.locator('#re-host').fill('bad host');
  await dialog.locator('#re-user').fill('robot');
  await dialog.locator('#re-token').fill(REPLACEMENT);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog.locator('#re-host-e')).toBeVisible();
  await expect(dialog.locator('#re-host')).toHaveValue('bad host');
  await expect(dialog.locator('#re-user')).toHaveValue('robot');
  await expect(dialog.locator('#re-token')).toHaveValue('');
  expect(await page.content()).not.toContain(REPLACEMENT);
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(registryRow(page, host).locator('th')).toHaveText(host);
  await expect(registryRow(page, host).locator('td').nth(0)).toHaveText('bot');
});

test('removing a login asks first, then refreshes the table and is audited', async ({
  page,
  host,
}) => {
  await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  const row = registryRow(page, host);
  if (!isWide(page)) {
    expect(await height(row.locator('[data-row-menu-button]'))).toBeGreaterThanOrEqual(
      TOUCH_TARGET,
    );
  }
  await (await rowAction(row, 'remove')).click();
  const dialog = page.locator('#registry-remove');
  await expect(dialog.locator('h2')).toHaveText(`Remove ${host}`);
  await expect(dialog).toContainText('is refused by the registry until a login is added again');
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(row).toBeVisible();

  await (await rowAction(row, 'remove')).click();
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).not.toHaveAttribute('open', '');
  await expect(row).toHaveCount(0);

  const details = await auditDetails(page, 'registry.remove', host);
  await expect(details).toContainText(host);
});

test('the dialog of a login that was removed meanwhile says so and the row goes away', async ({
  page,
  host,
}) => {
  const id = await addRegistry(page.request, host, 'bot', TOKEN);
  await page.goto('/settings/registries');
  const dialog = await openEditDialog(page, host);
  await removeRegistry(page.request, id);
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).toContainText('This registry is no longer in the list.');
  await expect(dialog.locator('button', { hasText: /^Close$/ })).toBeVisible();
  await expect(registryRow(page, host)).toHaveCount(0);
});
