import { expect, test, type Locator, type Page } from '@playwright/test';

import { horizontalOverflow } from '../viewport';
import { rowAction } from '../tests/row-actions';
import { submitSignIn } from '../tests/sign-in';

/*
 * Single sign-on against a real Keycloak (the committed dev realm of dev/keycloak/realm.json),
 * in a real browser: the superadmin sets it up in the Settings tab and then the realm's test
 * users sign in. Every value here is a development and testing one. The tests share the
 * instance's settings, so they run in order; a second project finds the settings of the first
 * one and sets them again.
 */
test.describe.configure({ mode: 'serial' });

const ISSUER = process.env.E2E_ISSUER ?? 'http://keycloak:8080/realms/envmanager-dev';
const APP_ORIGIN = new URL(process.env.E2E_BASE_URL ?? 'http://envmanager-sso:8080').origin;
const CLIENT_ID = 'env-manager';
const CLIENT_SECRET = 'dev-only-env-manager-client-secret';
const PASSWORD = process.env.E2E_PASSWORD ?? 'dev-only-password';
const ROOT = process.env.E2E_USER ?? 'root@example.test';

const NO_ROLE_MESSAGE = 'Your account at the identity provider has no role that may use ENV Manager.';
const UNVERIFIED_MESSAGE =
  'An account with your email address exists, but the identity provider has not verified the address.';

function card(page: Page): Locator {
  return page.locator('[data-fragment="login-card"]');
}

/** Open the password form of the sign-in card when single sign-on put it behind a disclosure. */
async function showPasswordForm(page: Page): Promise<void> {
  const disclosure = card(page).locator('details[data-fragment="password-sign-in"]');
  if ((await disclosure.count()) > 0 && !(await disclosure.evaluate((el) => (el as HTMLDetailsElement).open))) {
    await disclosure.locator('summary').click();
  }
  await expect(card(page).locator('#user')).toBeVisible();
}

async function signInWithPassword(page: Page, email: string, password: string): Promise<void> {
  await page.goto('/login');
  await showPasswordForm(page);
  await submitSignIn(page, email, password);
}

/** Start single sign-on from the card and answer the Keycloak sign-in page. */
async function signInWithSso(page: Page, username: string): Promise<void> {
  await page.goto('/login');
  await card(page).locator('a[href="/login/sso"]').click();
  await expect(page.locator('#kc-login')).toBeVisible();
  expect(new URL(page.url()).origin).toBe(new URL(ISSUER).origin);
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(`dev-only-${username}`);
  await page.locator('#kc-login').click();
  await page.waitForURL((url) => url.origin === APP_ORIGIN);
}

async function signOut(page: Page): Promise<void> {
  await page.locator('header [data-target="account-menu"]').click();
  await page.locator('#account-menu form[action="/logout"] button[type="submit"]').click();
  await expect(page).toHaveURL(/\/login$/);
}

function mappingRows(page: Page): Locator {
  return page.locator('#sso-mappings tr[data-fragment="sso-mapping-row"]');
}

async function setMapping(page: Page, rows: Array<[string, string]>): Promise<void> {
  while ((await mappingRows(page).count()) > 0) {
    const before = await mappingRows(page).count();
    await (await rowAction(mappingRows(page).first(), 'remove')).click();
    await expect(mappingRows(page)).toHaveCount(before - 1);
  }
  for (const [role, group] of rows) {
    const before = await mappingRows(page).count();
    await page.locator('#sso').getByRole('button', { name: 'Add mapping' }).click();
    await expect(mappingRows(page)).toHaveCount(before + 1);
    const row = mappingRows(page).nth(before);
    await row.locator('input[name="mapping_role"]').fill(role);
    await row.locator('select[name="mapping_group"]').selectOption(group);
  }
}

async function expectSignedInAs(page: Page, group: string): Promise<void> {
  await expect(page).toHaveURL(/\/projects$/);
  await page.goto('/account');
  await expect(page.locator('h1')).toHaveText('Account');
  await expect(page.locator(`[data-status="${group}"]`).first()).toBeVisible();
  for (const other of ['superadmin', 'admin', 'viewer'].filter((name) => name !== group)) {
    await expect(page.locator(`[data-status="${other}"]`)).toHaveCount(0);
  }
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
}

test('the superadmin turns single sign-on on in the Settings tab', async ({ page }) => {
  await signInWithPassword(page, ROOT, PASSWORD);
  await expect(page).toHaveURL(/\/projects$/);

  await page.goto('/settings/sso');
  const form = page.locator('#sso');
  if (await page.locator('#sso-enabled').isChecked()) {
    await page.locator('#sso-enabled').uncheck();
    await form.locator('button[type="submit"]').click();
    await expect(form.locator('[data-fragment="toast"]')).toBeVisible();
    await page.reload();
    await expect(page.locator('#sso-enabled')).not.toBeChecked();
  }
  await expect(page.locator('#sso-redirect-uri')).toHaveValue(`${APP_ORIGIN}/login/sso/callback`);
  await page.locator('#sso-issuer').fill(ISSUER);
  await page.locator('#sso-client-id').fill(CLIENT_ID);
  await page.locator('#sso-secret').fill(CLIENT_SECRET);
  await page.locator('input[name="create_users"]').check();
  await page.locator('input[name="link_by_email"]').check();
  await setMapping(page, [
    ['admin', 'admin'],
    ['viewer', 'viewer'],
  ]);

  await form.getByRole('button', { name: 'Test discovery' }).click();
  await expect(page.locator('#sso-test [data-status="deployed"]')).toBeVisible();
  await expect(page.locator('#sso-test')).toContainText(ISSUER);

  await page.locator('#sso-enabled').check();
  await form.getByRole('button', { name: 'Save and turn on' }).click();
  const dialog = page.locator('#sso-enable');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Save and turn on' }).click();
  await expect(form.locator('[data-fragment="toast"]')).toBeVisible();
  await page.reload();
  await expect(page.locator('#sso-enabled')).toBeChecked();

  await signOut(page);
  await expect(card(page).locator('a[href="/login/sso"]')).toBeVisible();
});

test('the sign-in card offers single sign-on first and never says who may use a password', async ({
  page,
}) => {
  await page.goto('/login');
  await expect(card(page).locator('a[href="/login/sso"]')).toBeVisible();
  await expect(card(page).locator('#user')).toBeHidden();
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  await showPasswordForm(page);
  expect(((await card(page).textContent()) ?? '').toLowerCase()).not.toContain('superadmin');
});

test('an admin of the realm signs in and the account is managed by single sign-on', async ({
  page,
}) => {
  await signInWithSso(page, 'sso-admin');
  await expectSignedInAs(page, 'admin');
  await expect(page.locator('[data-fragment="sso-account"]')).toBeVisible();
  await expect(page.locator('[data-fragment="account-profile"] [data-profile="name"]')).toHaveText(
    'SSO Admin',
  );
  await expect(page.locator('[data-fragment="account-profile"] [data-profile="email"]')).toHaveText(
    'sso-admin@example.test',
  );
  await expect(page.locator('#account-password [data-status="skipped"]')).toBeVisible();
  await expect(page.locator('#account-password form')).toHaveCount(0);
  await expect(page.locator('#totp [data-status="skipped"]')).toBeVisible();
  await expect(page.locator('#totp').getByRole('button', { name: 'Set up two-factor' })).toBeDisabled();
  await expect(page.locator('[data-sso-manage]')).toHaveAttribute('href', `${ISSUER}/account`);
});

test('a viewer of the realm signs in as a viewer', async ({ page }) => {
  await signInWithSso(page, 'sso-viewer');
  await expectSignedInAs(page, 'viewer');
});

test('a user without a role is refused with the fixed message', async ({ page }) => {
  await signInWithSso(page, 'sso-norole');
  await expect(page).toHaveURL(/\/login/);
  const message = page.locator('[data-fragment="login-message"]');
  await expect(message).toHaveAttribute('role', 'alert');
  await expect(message).toContainText(NO_ROLE_MESSAGE);
  expect(((await card(page).textContent()) ?? '').toLowerCase()).not.toContain('superadmin');
  await page.goto('/projects');
  await expect(page).toHaveURL(/\/login$/);
});

test('an unverified email of an existing account is refused linking', async ({ page }) => {
  await signInWithSso(page, 'sso-unverified');
  await expect(page).toHaveURL(/\/login/);
  await expect(page.locator('[data-fragment="login-message"]')).toContainText(UNVERIFIED_MESSAGE);
  await page.goto('/projects');
  await expect(page).toHaveURL(/\/login$/);
});

test('the realm user of the superadmin email signs in and stays superadmin', async ({ page }) => {
  await signInWithSso(page, 'sso-root');
  await expectSignedInAs(page, 'superadmin');
  await expect(page.locator('[data-fragment="sso-account"]')).toBeVisible();
  await expect(page.locator('#account-password form')).toHaveCount(1);
  await expect(page.locator('[data-fragment="account-profile"]')).toHaveCount(0);
  await expect(page.locator('#totp [data-status="skipped"]')).toHaveCount(0);
  await expect(page.locator('#totp').getByRole('button', { name: 'Set up two-factor' })).toBeEnabled();
  await page.goto('/settings/sso');
  await expect(page.locator('#sso')).toBeVisible();
});

test('an admin with the right password is refused while single sign-on is on', async ({ page }) => {
  await signInWithPassword(page, 'admin@example.test', PASSWORD);
  const message = page.locator('[data-fragment="login-message"]');
  await expect(message).toContainText('The email or password is incorrect.');
  await expect(page).toHaveURL(/\/login$/);
  expect(((await card(page).textContent()) ?? '').toLowerCase()).not.toContain('superadmin');
});

test('the superadmin still signs in with the password', async ({ page }) => {
  await signInWithPassword(page, ROOT, PASSWORD);
  await expect(page).toHaveURL(/\/projects$/);
});
