import { expect, test as base, type Page } from '@playwright/test';

import { stateFile } from '../global-setup';
import { createUser, disableUser, uniqueUserName, type CreatedUser } from '../users-api';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';
import { signedInAs } from './sign-in';
import { totpCode } from './totp';

/*
 * The account page. Every flow that changes a password, the second factor or a session runs on a
 * scratch user created through the app and disabled when the test ends: the seeded users and the
 * stored sessions other specs sign in with are only read here. Wrong passwords and codes count on
 * the per-address lockout counter that every spec shares (the development stack raises that limit
 * far above what a full run uses), so the spec makes only the few wrong credentials it needs per
 * project; refusals that need no credential check (empty fields) cover the other error states.
 */

const NEW_PASSWORD = 'a brand new passphrase';
const SECRET_FORMAT = /^[A-Z2-7]{4}( [A-Z2-7]{4}){7}$/;

/** The base test plus `makeUser`: scratch users created through the form post, disabled after. */
const test = base.extend<{ makeUser: (group?: 'admin' | 'viewer' | 'superadmin') => Promise<CreatedUser> }>({
  makeUser: async ({ request }, use, testInfo) => {
    const created: CreatedUser[] = [];
    await use(async (group = 'admin') => {
      const user = await createUser(request, uniqueUserName(testInfo.project.name), group);
      created.push(user);
      return user;
    });
    for (const user of created) {
      await disableUser(request, user.id);
    }
  },
});

async function openAccount(page: Page): Promise<void> {
  await page.goto('/account');
  await expect(page.locator('h1')).toHaveText(/Account/);
}

const sessionRows = (page: Page) => page.locator('#account-sessions tr[data-fragment="session-row"]');

async function startSetup(page: Page): Promise<string> {
  await page.locator('#totp button[hx-post="/account/totp/setup"]').click();
  await expect(page.locator('#totp-qr')).toBeVisible();
  const text = (await page.locator('#totp-secret').textContent()) ?? '';
  expect(text).toMatch(SECRET_FORMAT);
  return text.replace(/ /g, '');
}

async function enableTotp(page: Page, user: CreatedUser): Promise<string> {
  await openAccount(page);
  const secret = await startSetup(page);
  await page.locator('#tp').fill(user.password);
  await page.locator('#tc').fill(totpCode(secret));
  await page.locator('#account-totp-form button[type="submit"]').click();
  await expect(page.locator('#totp [data-status="deployed"]')).toHaveText('Enabled');
  return secret;
}

test.describe('the page for every group', () => {
  for (const user of ['viewer', 'admin'] as const) {
    test.describe(`as the seeded ${user}`, () => {
      test.use({ storageState: stateFile(user) });

      test('shows the four panels and the current session', async ({ page }) => {
        await openAccount(page);
        await expect(page.locator('[data-fragment="page-header"] p')).toContainText(`${user} ·`);
        for (const fragment of ['account-password', 'account-totp', 'account-sessions', 'account-theme']) {
          await expect(page.locator(`[data-fragment="${fragment}"]`)).toBeVisible();
        }
        await expect(page.locator('#totp [data-status="never"]')).toHaveText('Not enabled');
        await expect(page.locator('#account-sessions [data-current="true"]')).toContainText('This session');
        await expect(page.locator(`#account-sessions [data-current="true"] button`)).toHaveCount(0);
        expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
      });
    });
  }

  test('shows the four panels to the superadmin, from the menu link', async ({ page }) => {
    await page.goto('/projects');
    await page.locator('header [data-target="account-menu"]').click();
    await page.locator('#account-menu a[href="/account"]').click();
    await expect(page).toHaveURL(/\/account$/);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText(/Account/);
    await expect(page.locator('[data-fragment="page-header"] [data-status="superadmin"]')).toBeVisible();
    await expect(page.locator('#account-sessions [data-current="true"]')).toContainText('This session');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      for (const button of await page.locator('main button[type="submit"], main button[hx-post]').all()) {
        expect((await button.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET - 1);
      }
    }
  });

  test('an account that is not linked to single sign-on has no note and every control', async ({
    page,
  }) => {
    await openAccount(page);
    await expect(page.locator('[data-fragment="sso-account"]')).toHaveCount(0);
    await expect(page.locator('[data-sso-manage]')).toHaveCount(0);
    await expect(page.locator('[data-fragment="account-profile"]')).toHaveCount(0);
    await expect(page.locator('#account-password-form')).toBeVisible();
    await expect(page.locator('#totp button[hx-post="/account/totp/setup"]')).toBeEnabled();
  });

  test('a signed out visitor is sent to the sign in page', async ({ browser, baseURL }) => {
    const context = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] } });
    const page = await context.newPage();
    await page.goto('/account');
    await expect(page).toHaveURL(/\/login$/);
    await context.close();
  });
});

test.describe('change password', () => {
  test('refuses each bad input with a message next to its field and changes nothing', async ({
    browser,
    makeUser,
  }) => {
    const user = await makeUser('admin');
    const page = await signedInAs(browser, user);
    await openAccount(page);
    const form = page.locator('#account-password-form');
    const submit = form.locator('button[type="submit"]');

    await submit.click();
    await expect(page.locator('#cp-e')).toHaveText('Enter your current password.');

    await page.locator('#cp').fill(user.password);
    await page.locator('#np').fill('short');
    await page.locator('#cp2').fill('short');
    await page.locator('#account-password-form button[type="submit"]').click();
    await expect(page.locator('#np-e')).toContainText('at least 12 characters');
    await expect(page.locator('#cp')).toHaveValue('');

    await page.locator('#cp').fill(user.password);
    await page.locator('#np').fill(NEW_PASSWORD);
    await page.locator('#cp2').fill('another new passphrase');
    await page.locator('#account-password-form button[type="submit"]').click();
    await expect(page.locator('#cp2-e')).toHaveText('The passwords do not match.');

    await page.locator('#cp').fill(user.password);
    await page.locator('#np').fill(user.password);
    await page.locator('#cp2').fill(user.password);
    await page.locator('#account-password-form button[type="submit"]').click();
    await expect(page.locator('#np-e')).toContainText('must differ from the current one');

    await page.locator('#cp').fill('not my password');
    await page.locator('#np').fill(NEW_PASSWORD);
    await page.locator('#cp2').fill(NEW_PASSWORD);
    await page.locator('#account-password-form button[type="submit"]').click();
    await expect(page.locator('#cp-e')).toHaveText('The current password is incorrect.');
    await expect(page.locator('#np')).toHaveValue('');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    const again = await signedInAs(browser, user);
    await again.context().close();
    await page.context().close();
  });

  test('changes the password, signs the other session out and keeps this one', async ({
    browser,
    makeUser,
  }) => {
    const user = await makeUser('viewer');
    const page = await signedInAs(browser, user);
    const other = await signedInAs(browser, user);
    await openAccount(page);
    await expect(sessionRows(page)).toHaveCount(2);

    await page.locator('#cp').fill(user.password);
    await page.locator('#np').fill(NEW_PASSWORD);
    await page.locator('#cp2').fill(NEW_PASSWORD);
    await page.locator('#account-password-form button[type="submit"]').click();
    const done = page.locator('#account-password [role="status"]');
    await expect(done).toContainText('Password changed. 1 other session was signed out.');
    await expect(page.locator('#cp')).toHaveValue('');
    await expect(page.locator('#np')).toHaveValue('');
    await expect(sessionRows(page)).toHaveCount(1);
    await expect(sessionRows(page).first()).toHaveAttribute('data-current', 'true');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await other.goto('/projects');
    await expect(other).toHaveURL(/\/login\?expired=1$/);
    await page.goto('/projects');
    await expect(page).toHaveURL(/\/projects$/);

    const fresh = await signedInAs(browser, user, { password: NEW_PASSWORD });
    await fresh.context().close();
    await other.context().close();
    await page.context().close();
  });
});

test.describe('two-factor authentication', () => {
  test('sets up with a QR code and the key, refuses a wrong code and turns on with a right one', async ({
    browser,
    makeUser,
  }) => {
    const user = await makeUser('admin');
    const page = await signedInAs(browser, user);
    await openAccount(page);
    await expect(page.locator('#totp [data-status="never"]')).toHaveText('Not enabled');

    const first = await startSetup(page);
    const qr = page.locator('#totp-qr');
    await expect(qr).toHaveAttribute('src', /^\/account\/totp\/qr\.svg\?v=[\w-]+$/);
    await expect
      .poll(() => qr.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0))
      .toBe(true);
    const svg = await page.request.get((await qr.getAttribute('src')) ?? '');
    expect(svg.headers()['content-type']).toBe('image/svg+xml');
    expect(svg.headers()['cache-control']).toBe('no-store');
    expect((await page.request.get('/account/totp/qr.svg?v=other')).status()).toBe(404);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.locator('#totp button[hx-get="/account/totp"]').click();
    await expect(page.locator('#totp [data-status="never"]')).toBeVisible();
    const second = await startSetup(page);
    expect(second).not.toBe(first);

    const submit = page.locator('#account-totp-form button[type="submit"]');
    const shownSecret = async () =>
      (await page.locator('#totp-secret').textContent())?.replace(/ /g, '');
    await submit.click();
    await expect(page.locator('#tp-e')).toHaveText('Enter your current password.');
    await page.locator('#tp').fill(user.password);
    await submit.click();
    await expect(page.locator('#tc-e')).toHaveText('Enter the 6-digit code.');
    await expect(page.locator('#tp')).toHaveValue('');

    await page.locator('#tp').fill(user.password);
    await page.locator('#tc').fill('000000');
    await submit.click();
    await expect(page.locator('#tc-e')).toHaveText('The code is incorrect or expired.');
    expect(await shownSecret()).toBe(second);
    await expect(page.locator('#tp')).toHaveValue('');
    await expect(page.locator('#tc')).toHaveValue('');

    await page.locator('#tp').fill('not the password at all');
    await page.locator('#tc').fill(totpCode(second));
    await submit.click();
    await expect(page.locator('#tp-e')).toHaveText('The current password is incorrect.');
    expect(await shownSecret()).toBe(second);
    await expect(page.locator('#tp')).toHaveValue('');
    await expect(page.locator('#totp [data-status="deployed"]')).toHaveCount(0);

    await page.locator('#tp').fill(user.password);
    await page.locator('#tc').fill(totpCode(second));
    await submit.click();
    await expect(page.locator('#totp [data-status="deployed"]')).toHaveText('Enabled');
    await expect(page.locator('#totp')).toContainText('Authenticator app configured on');
    await expect(page.locator('#totp-secret')).toHaveCount(0);
    await expect(page.locator('#totp button[data-target="totp-remove"]')).toBeVisible();

    await page.reload();
    await expect(page.locator('#totp [data-status="deployed"]')).toHaveText('Enabled');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await page.context().close();
  });

  test('a setup that ran out is dropped with a note and can be started again', async ({
    browser,
    makeUser,
  }) => {
    const user = await makeUser('viewer');
    const page = await signedInAs(browser, user);
    await openAccount(page);
    await startSetup(page);
    await page.context().clearCookies({ name: 'envmanager_totp_setup' });
    await page.locator('#tc').fill('123456');
    await page.locator('#account-totp-form button[type="submit"]').click();
    await expect(page.locator('#totp [role="status"]')).toContainText(
      'The setup took too long and was dropped. Start again.',
    );
    await expect(page.locator('#totp-secret')).toHaveCount(0);
    await expect(page.locator('#totp button[hx-post="/account/totp/setup"]')).toBeVisible();
    expect(await startSetup(page)).toMatch(/^[A-Z2-7]{32}$/);
    await page.context().close();
  });

  test('is asked for at the next sign in', async ({ browser, makeUser }) => {
    const user = await makeUser('viewer');
    const page = await signedInAs(browser, user);
    const secret = await enableTotp(page, user);
    await page.context().close();

    const nextStep = totpCode(secret, Date.now() + 30000);
    const again = await signedInAs(browser, user, { code: nextStep });
    await again.context().close();
  });

  test('is removed with the password and a fresh code after a confirm dialog', async ({
    browser,
    makeUser,
  }) => {
    const user = await makeUser('admin');
    const page = await signedInAs(browser, user);
    const secret = await enableTotp(page, user);

    await page.locator('#totp button[data-target="totp-remove"]').click();
    const dialog = page.locator('#totp-remove[open]');
    await expect(dialog.locator('#rp')).toBeVisible();
    await expect(dialog.locator('#rc')).toBeVisible();
    await expect(dialog.locator('#totp-remove-form button[type="submit"]')).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await dialog.locator('#totp-remove-form button[type="submit"]').click();
    await expect(dialog.locator('#rp-e')).toHaveText('Enter your current password.');
    await dialog.locator('#rp').fill(user.password);
    await dialog.locator('#totp-remove-form button[type="submit"]').click();
    await expect(dialog.locator('#rc-e')).toHaveText('Enter the 6-digit code.');
    await expect(dialog.locator('#rp')).toHaveValue('');
    await dialog.locator('button[data-js="dialog-close"]').first().click();
    await expect(page.locator('#totp-remove[open]')).toHaveCount(0);
    await expect(page.locator('#totp [data-status="deployed"]')).toHaveText('Enabled');

    await page.locator('#totp button[data-target="totp-remove"]').click();
    await expect(page.locator('#totp-remove[open] #rp')).toHaveValue('');
    await page.locator('#rp').fill(user.password);
    await page.locator('#rc').fill(totpCode(secret, Date.now() + 30000));
    await page.locator('#totp-remove-form button[type="submit"]').click();
    await expect(page.locator('#totp-remove[open]')).toHaveCount(0);
    await expect(page.locator('#totp [data-status="never"]')).toHaveText('Not enabled');
    await expect(page.locator('#totp button[hx-post="/account/totp/setup"]')).toBeVisible();

    await page.reload();
    await expect(page.locator('#totp [data-status="never"]')).toBeVisible();
    await page.context().close();
  });
});

test.describe('active sessions', () => {
  test('lists this session first, revokes another after a confirm dialog and signs out the rest', async ({
    browser,
    makeUser,
  }) => {
    const user = await makeUser('viewer');
    const page = await signedInAs(browser, user);
    const second = await signedInAs(browser, user);
    const third = await signedInAs(browser, user);
    await openAccount(page);

    const rows = sessionRows(page);
    await expect(rows).toHaveCount(3);
    await expect(rows.first()).toHaveAttribute('data-current', 'true');
    await expect(rows.first()).toContainText('This session');
    await expect(rows.first()).toContainText('Signed in');
    await expect(rows.first().locator('button')).toHaveCount(0);
    await expect(page.locator('#account-sessions button[hx-get$="/revoke"]')).toHaveCount(2);
    await expect(page.locator('#account-sessions th[scope="col"]')).toHaveText([
      'Device',
      'IP',
      'Last active',
      'Actions',
    ]);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.locator('#account-sessions button[hx-get$="/revoke"]').first().click();
    const dialog = page.locator('#session-revoke[open]');
    await expect(dialog.locator('button[type="submit"]')).toHaveText('Sign out session');
    await dialog.locator('button[data-js="dialog-close"]').last().click();
    await expect(rows).toHaveCount(3);

    await page.locator('#account-sessions button[hx-get$="/revoke"]').first().click();
    await page.locator('#session-revoke[open] button[type="submit"]').click();
    await expect(page.locator('#session-revoke[open]')).toHaveCount(0);
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toHaveAttribute('data-current', 'true');

    const dead = [second, third];
    const states = await Promise.all(
      dead.map(async (other) => {
        await other.goto('/projects');
        return new URL(other.url()).pathname;
      }),
    );
    expect(states.sort()).toEqual(['/login', '/projects']);

    await page.locator('#account-sessions button[hx-get="/account/sessions/revoke-others"]').click();
    const everyone = page.locator('#session-revoke[open]');
    await expect(everyone).toContainText('1 other session ends at once. This session stays signed in.');
    await everyone.locator('button[type="submit"]').click();
    await expect(page.locator('#session-revoke[open]')).toHaveCount(0);
    await expect(rows).toHaveCount(1);
    await expect(page.locator('#account-sessions button[hx-get="/account/sessions/revoke-others"]')).toHaveCount(0);

    for (const other of dead) {
      await other.goto('/projects');
      await expect(other).toHaveURL(/\/login/);
    }
    await page.goto('/projects');
    await expect(page).toHaveURL(/\/projects$/);
    for (const opened of [page, second, third]) {
      await opened.context().close();
    }
  });
});

test.describe('theme preference', () => {
  const stored = (page: Page) => page.evaluate(() => localStorage.getItem('env-theme'));

  test('keeps the choice in the browser and applies it', async ({ page }) => {
    await openAccount(page);
    const root = page.locator('html');
    const option = (value: string) => page.locator(`#account-theme input[value="${value}"]`);
    await expect(option('system')).toBeChecked();

    await option('dark').check();
    await expect(root).toHaveClass(/dark/);
    expect(await stored(page)).toBe('dark');
    await page.reload();
    await expect(root).toHaveClass(/dark/);
    await expect(option('dark')).toBeChecked();

    await option('light').check();
    await expect(root).not.toHaveClass(/dark/);
    expect(await stored(page)).toBe('light');

    await page.emulateMedia({ colorScheme: 'dark' });
    await option('system').check();
    await expect(root).toHaveClass(/dark/);
    expect(await stored(page)).toBe('system');
    await page.emulateMedia({ colorScheme: 'light' });
    await expect(root).not.toHaveClass(/dark/);
    await page.reload();
    await expect(option('system')).toBeChecked();
    await expect(root).not.toHaveClass(/dark/);
    await page.evaluate(() => localStorage.removeItem('env-theme'));
  });

  test('is Match system when nothing is stored and follows the operating system', async ({
    page,
  }) => {
    await openAccount(page);
    const root = page.locator('html');
    await page.evaluate(() => localStorage.removeItem('env-theme'));

    await page.emulateMedia({ colorScheme: 'dark' });
    await page.reload();
    await expect(root).toHaveClass(/dark/);
    await expect(page.locator('#account-theme input[value="system"]')).toBeChecked();
    await expect(page.locator('#account-theme input:checked')).toHaveCount(1);
    expect(await stored(page)).toBeNull();

    await page.emulateMedia({ colorScheme: 'light' });
    await expect(root).not.toHaveClass(/dark/);
    await page.reload();
    await expect(root).not.toHaveClass(/dark/);
    await expect(page.locator('#account-theme input[value="system"]')).toBeChecked();
    expect(await stored(page)).toBeNull();
  });

  test('follows the theme buttons of the page', async ({ page }) => {
    await openAccount(page);
    const root = page.locator('html');
    await page.locator('header [data-js="theme-toggle"]').click();
    await expect(root).toHaveClass(/dark/);
    await expect(page.locator('#account-theme input[value="dark"]')).toBeChecked();
    await page.locator('header [data-js="theme-toggle"]').click();
    await expect(root).not.toHaveClass(/dark/);
    await expect(page.locator('#account-theme input[value="light"]')).toBeChecked();
    await page.evaluate(() => localStorage.removeItem('env-theme'));
  });
});
