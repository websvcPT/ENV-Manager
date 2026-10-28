import { type Locator, type Page } from '@playwright/test';

import { test } from '../settings-test';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';
import { rowAction } from './row-actions';

const { expect } = test;
const SECRET = 'e2e-sso-client-secret-sentinel';
const UNREACHABLE = 'http://127.0.0.1:1/realms/e2e';

/*
 * The Single sign-on tab on the development stack. A spec never turns sign in on there: the
 * confirmation is opened and cancelled, and the saves leave it off, so the sign-in card and the
 * password form stay as the other specs expect. A spec that saves holds the settings lock and puts
 * back what it found; enabling and the whole sign in are covered by the web tests and by the
 * Keycloak run. The unreachable issuer is a closed port on the app's own host, which refuses at
 * once.
 */

interface SsoValues {
  issuer: string;
  clientId: string;
  rolesClaim: string;
  accountUrl: string;
  create: boolean;
  link: boolean;
  rows: Array<[string, string]>;
}

async function height(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box?.height ?? 0;
}

function form(page: Page): Locator {
  return page.locator('#sso');
}

function toast(scope: Locator): Locator {
  return scope.locator('[data-fragment="toast"]');
}

function mappingRows(page: Page): Locator {
  return page.locator('#sso-mappings tr[data-fragment="sso-mapping-row"]');
}

async function save(page: Page): Promise<void> {
  await form(page).locator('button[type="submit"]').click();
}

async function addRow(page: Page, role: string, group: string): Promise<void> {
  const before = await mappingRows(page).count();
  await form(page).getByRole('button', { name: 'Add mapping' }).click();
  await expect(mappingRows(page)).toHaveCount(before + 1);
  const row = mappingRows(page).nth(before);
  await row.locator('input[name="mapping_role"]').fill(role);
  await row.locator('select[name="mapping_group"]').selectOption(group);
}

async function removeAllRows(page: Page): Promise<void> {
  while ((await mappingRows(page).count()) > 0) {
    const before = await mappingRows(page).count();
    await (await rowAction(mappingRows(page).first(), 'remove')).click();
    await expect(mappingRows(page)).toHaveCount(before - 1);
  }
}

async function readValues(page: Page): Promise<SsoValues> {
  const rows: Array<[string, string]> = [];
  for (const row of await mappingRows(page).all()) {
    rows.push([
      await row.locator('input[name="mapping_role"]').inputValue(),
      await row.locator('select[name="mapping_group"]').inputValue(),
    ]);
  }
  return {
    issuer: await page.locator('#sso-issuer').inputValue(),
    clientId: await page.locator('#sso-client-id').inputValue(),
    rolesClaim: await page.locator('#sso-roles-claim').inputValue(),
    accountUrl: await page.locator('#sso-account-url').inputValue(),
    create: await page.locator('input[name="create_users"]').isChecked(),
    link: await page.locator('input[name="link_by_email"]').isChecked(),
    rows,
  };
}

async function fillValues(page: Page, values: SsoValues): Promise<void> {
  await page.locator('#sso-issuer').fill(values.issuer);
  await page.locator('#sso-client-id').fill(values.clientId);
  await page.locator('#sso-roles-claim').fill(values.rolesClaim);
  await page.locator('#sso-account-url').fill(values.accountUrl);
  await page.locator('input[name="create_users"]').setChecked(values.create);
  await page.locator('input[name="link_by_email"]').setChecked(values.link);
  await removeAllRows(page);
  for (const [role, group] of values.rows) {
    await addRow(page, role, group);
  }
}

/** Put the form back to what it was when the test began, and a stored secret that was not there. */
async function restore(page: Page, original: SsoValues, hadSecret: boolean): Promise<void> {
  await page.goto('/settings/sso');
  if (!hadSecret) {
    const remove = page.locator('input[name="remove_client_secret"]');
    if ((await remove.count()) > 0) {
      await remove.check();
    }
  }
  await fillValues(page, original);
  await save(page);
  await expect(toast(form(page))).toBeVisible();
}

test.describe('the tab', () => {
  test('shows the three panels, the fields, the redirect address and the actions', async ({
    page,
  }) => {
    await page.goto('/settings/sso');
    await expect(page).toHaveTitle('Settings · Single sign-on · ENV Manager');
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'Single sign-on',
    );
    await expect(form(page).locator('section h2')).toHaveText([
      'Identity provider',
      'Group mapping',
      'Accounts',
    ]);
    await expect(page.locator('label[for="sso-issuer"]')).toHaveText('Issuer URL');
    await expect(page.locator('label[for="sso-client-id"]')).toHaveText('Client ID');
    await expect(page.locator('label[for="sso-secret"]')).toHaveText('Client secret');
    await expect(page.locator('label[for="sso-account-url"]')).toHaveText(
      'Account management URL',
    );
    await expect(page.locator('label[for="sso-roles-claim"]')).toHaveText('Roles claim');
    await expect(page.locator('#sso-secret')).toHaveAttribute('type', 'password');
    await expect(page.locator('#sso-secret')).toHaveValue('');
    await expect(page.locator('#sso-enabled')).not.toBeChecked();
    const redirect = page.locator('#sso-redirect-uri');
    await expect(redirect).toHaveAttribute('readonly', '');
    expect(await redirect.inputValue()).toMatch(/^https?:\/\/.+\/login\/sso\/callback$/);
    await expect(form(page).locator('[data-js="copy"][data-target="sso-redirect-uri"]')).toBeVisible();
    await expect(form(page).getByRole('button', { name: 'Test discovery' })).toBeVisible();
    await expect(form(page).getByRole('button', { name: 'Add mapping' })).toBeVisible();
    await expect(form(page).locator('button[type="submit"]')).toHaveText('Save settings');
    await expect(form(page).getByRole('button', { name: 'Save and turn on' })).toBeHidden();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('keeps buttons and fields at a touch size on a phone', async ({ page }) => {
    await page.goto('/settings/sso');
    await addRow(page, 'e2e-role', 'viewer');
    if (!isWide(page)) {
      for (const name of ['Test discovery', 'Add mapping']) {
        expect(await height(form(page).getByRole('button', { name }))).toBeGreaterThanOrEqual(
          TOUCH_TARGET,
        );
      }
      expect(await height(form(page).locator('button[type="submit"]'))).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
    }
    for (const field of ['#sso-issuer', '#sso-client-id', '#sso-secret', '#sso-redirect-uri']) {
      expect(await height(page.locator(field))).toBeGreaterThanOrEqual(isWide(page) ? 37 : 40);
    }
    const row = mappingRows(page).first();
    expect(await height(row.locator('select'))).toBeGreaterThanOrEqual(isWide(page) ? 37 : 40);
    expect(await height(row.locator('input'))).toBeGreaterThanOrEqual(isWide(page) ? 37 : 40);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('mapping rows', () => {
  test('are added and removed on the server and keep what else was typed', async ({ page }) => {
    await page.goto('/settings/sso');
    await removeAllRows(page);
    await expect(page.locator('#sso-mappings')).toContainText('No rows yet.');
    await page.locator('#sso-secret').fill(SECRET);
    await addRow(page, 'env-admin', 'admin');
    await addRow(page, 'env-viewer', 'viewer');
    await expect(mappingRows(page).locator('th')).toHaveText(['1', '2']);
    await expect(page.locator('#sso-secret')).toHaveValue(SECRET);
    await (await rowAction(mappingRows(page).first(), 'remove')).click();
    await expect(mappingRows(page)).toHaveCount(1);
    await expect(mappingRows(page).first().locator('input[name="mapping_role"]')).toHaveValue(
      'env-viewer',
    );
    await expect(mappingRows(page).first().locator('th')).toHaveText('1');
    await expect(
      mappingRows(page).first().locator('select[name="mapping_group"] option'),
    ).toHaveText(['Admin', 'Viewer']);
    await expect(page.locator('#sso-secret')).toHaveValue(SECRET);
    expect(await page.content()).toContain('name="mapping_role"');
    await page.reload();
    await expect(page.locator('#sso-secret')).toHaveValue('');
  });
});

test.describe('mapping row order', () => {
  test('a row moves up and down, and the order is the order of the rows', async ({ page }) => {
    await page.goto('/settings/sso');
    await removeAllRows(page);
    await addRow(page, 'first', 'admin');
    await addRow(page, 'second', 'viewer');
    await addRow(page, 'third', 'viewer');
    const roles = async (): Promise<string[]> =>
      mappingRows(page)
        .locator('input[name="mapping_role"]')
        .evaluateAll((inputs) => inputs.map((input) => (input as HTMLInputElement).value));
    await (await rowAction(mappingRows(page).nth(0), 'down')).click();
    await expect.poll(roles).toEqual(['second', 'first', 'third']);
    await (await rowAction(mappingRows(page).nth(2), 'up')).click();
    await expect.poll(roles).toEqual(['second', 'third', 'first']);
    await expect(mappingRows(page).nth(2).locator('select')).toHaveValue('admin');
    await expect(mappingRows(page).locator('th')).toHaveText(['1', '2', '3']);
  });
});

test.describe('saving', () => {
  test('saves the settings with sign in off, shows them after a reload and puts them back', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/sso');
    const original = await readValues(page);
    const hadSecret = (await page.locator('input[name="remove_client_secret"]').count()) > 0;
    try {
      const mine: SsoValues = {
        issuer: UNREACHABLE,
        clientId: 'e2e-client',
        rolesClaim: 'groups',
        accountUrl: 'http://127.0.0.1:1/account',
        create: !original.create,
        link: !original.link,
        rows: [
          ['e2e-admin', 'admin'],
          ['e2e-viewer', 'viewer'],
        ],
      };
      await fillValues(page, mine);
      if (!hadSecret) {
        await page.locator('#sso-secret').fill(SECRET);
      }
      await save(page);
      const saved = toast(form(page));
      await expect(saved).toContainText('Single sign-on settings saved');
      await expect(saved).toContainText('They apply to the next sign in.');
      await expect(page.locator('#sso-enabled')).not.toBeChecked();
      await page.reload();
      expect(await readValues(page)).toEqual(mine);
      await expect(page.locator('#sso-secret')).toHaveValue('');
      await expect(page.locator('#sso-secret')).toHaveAttribute('placeholder', '••••••••');
      await expect(page.locator('#sso-secret-h')).toHaveText(
        'Stored encrypted. Leave empty to keep it.',
      );
      expect(await page.content()).not.toContain(SECRET);
      await save(page);
      await expect(toast(form(page))).toContainText('Nothing to save');
    } finally {
      await restore(page, original, hadSecret);
    }
    await page.goto('/settings/sso');
    expect(await readValues(page)).toEqual(original);
    await expect(page.locator('input[name="remove_client_secret"]')).toHaveCount(hadSecret ? 1 : 0);
  });

  test('refuses bad values with the reason next to each field and stores nothing', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/sso');
    const original = await readValues(page);
    await page.locator('#sso-issuer').fill('ftp://nope');
    await page.locator('#sso-roles-claim').fill('a..b');
    await page.locator('#sso-account-url').fill('not an address');
    await addRow(page, '', 'viewer');
    await save(page);
    await expect(page.locator('#sso-issuer-e')).toHaveText(
      'Enter an http or https address that names a host.',
    );
    await expect(page.locator('#sso-roles-claim-e')).toContainText('Enter a dotted path');
    await expect(page.locator('#sso-account-url-e')).toBeVisible();
    await expect(page.locator('#sso-mappings-e')).toContainText('enter the role');
    await expect(toast(form(page))).toHaveCount(0);
    await page.reload();
    expect(await readValues(page)).toEqual(original);
  });
});

test.describe('turning sign in on', () => {
  test('a refused setup names what is missing and the confirmation can be cancelled', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/sso');
    await removeAllRows(page);
    await page.locator('#sso-issuer').fill('');
    await page.locator('#sso-client-id').fill('');
    await page.locator('#sso-enabled').check();
    await expect(form(page).locator('button[type="submit"]')).toBeHidden();
    const turnOn = form(page).getByRole('button', { name: 'Save and turn on' });
    await expect(turnOn).toBeVisible();
    await turnOn.click();
    await expect(page.locator('#sso-enabled-e')).toHaveText(
      'Complete the settings below before turning single sign-on on.',
    );
    await expect(page.locator('#sso-issuer-e')).toBeVisible();
    await expect(page.locator('#sso-client-id-e')).toBeVisible();
    await expect(page.locator('#sso-mappings-e')).toHaveText('Add at least one mapping row.');
    await expect(page.locator('#sso-enable')).not.toBeVisible();

    await page.locator('#sso-issuer').fill(UNREACHABLE);
    await page.locator('#sso-client-id').fill('e2e-client');
    await page.locator('#sso-secret').fill(SECRET);
    await addRow(page, 'e2e-admin', 'admin');
    await form(page).getByRole('button', { name: 'Save and turn on' }).click();
    const dialog = page.locator('#sso-enable');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('h2')).toHaveText('Turn on single sign-on');
    await expect(dialog).toContainText('Admins and viewers can then sign in only with SSO');
    await expect(dialog).toContainText('Superadmins keep password sign in');
    await expect(dialog.getByRole('button', { name: 'Save and turn on' })).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).not.toBeVisible();
    await page.reload();
    await expect(page.locator('#sso-enabled')).not.toBeChecked();
    await expect(page.locator('#sso-issuer')).not.toHaveValue(UNREACHABLE);
  });
});

test.describe('test discovery', () => {
  test('says why an unreachable provider fails and saves nothing', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/sso');
    const before = await page.locator('#sso-issuer').inputValue();
    await page.locator('#sso-issuer').fill(UNREACHABLE);
    await form(page).getByRole('button', { name: 'Test discovery' }).click();
    const result = page.locator('#sso-test');
    await expect(result.locator('[data-status="failed"]')).toHaveText('Discovery failed');
    await expect(result).toContainText('The identity provider could not be reached.');
    await page.locator('#sso-issuer').fill('ftp://nope');
    await form(page).getByRole('button', { name: 'Test discovery' }).click();
    await expect(result).toContainText('Enter an http or https address that names a host.');
    await page.reload();
    await expect(page.locator('#sso-issuer')).toHaveValue(before);
  });
});
