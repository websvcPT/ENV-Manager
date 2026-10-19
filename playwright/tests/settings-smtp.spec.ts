import { type Locator, type Page } from '@playwright/test';

import { htmlText, plainText, waitForMailTo } from '../mail-api';
import { test } from '../settings-test';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';

const { expect } = test;
const PASSWORD = 'e2e-smtp-password-sentinel';
const BANNER = 'SMTP is not configured: no notification emails are sent.';

/*
 * The SMTP tab on the development stack, whose SMTP settings point at the smtp4dev mail catcher
 * (the development seed does that when SMTP is not configured). A spec that saves a setting holds
 * the settings lock and puts back what it found, so it works whatever the stack was set to.
 * Mail that the application sends is read back from the catcher's own API.
 */

interface SmtpForm {
  host: string;
  port: string;
  tls: string;
  username: string;
  from: string;
}

async function height(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box?.height ?? 0;
}

function form(page: Page): Locator {
  return page.locator('#smtp');
}

function toast(scope: Locator): Locator {
  return scope.locator('[data-fragment="toast"]');
}

function banners(page: Page): Locator {
  return page.locator('#banner-area').getByText(BANNER);
}

async function readForm(page: Page): Promise<SmtpForm> {
  return {
    host: await page.locator('#sh').inputValue(),
    port: await page.locator('#sp').inputValue(),
    tls: await page.locator('#tls').inputValue(),
    username: await page.locator('#su').inputValue(),
    from: await page.locator('#sf').inputValue(),
  };
}

async function fillForm(page: Page, values: SmtpForm): Promise<void> {
  await page.locator('#sh').fill(values.host);
  await page.locator('#sp').fill(values.port);
  await page.locator('#tls').selectOption(values.tls);
  await page.locator('#su').fill(values.username);
  await page.locator('#sf').fill(values.from);
}

async function save(page: Page): Promise<void> {
  await form(page).locator('button[type="submit"]').click();
}

/** Remove the stored password, when there is one. */
async function removeStoredPassword(page: Page): Promise<void> {
  await page.goto('/settings/smtp');
  const remove = page.locator('input[name="remove_password"]');
  if ((await remove.count()) > 0) {
    await remove.check();
    await save(page);
    await expect(toast(form(page))).toBeVisible();
  }
}

/** Put the form back to what it was when the test began. */
async function restore(page: Page, original: SmtpForm): Promise<void> {
  await page.goto('/settings/smtp');
  await fillForm(page, original);
  await save(page);
  await expect(toast(form(page))).toBeVisible();
}

test.describe('the tab', () => {
  test('shows the server fields, the test mail row and the save button', async ({ page }) => {
    await page.goto('/settings/smtp');
    await expect(page).toHaveTitle('Settings · SMTP · ENV Manager');
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'SMTP',
    );
    const panel = page.locator('[data-fragment="settings-smtp"]');
    await expect(panel.locator('h2')).toHaveText('SMTP');
    await expect(panel.locator('label[for="sh"]')).toHaveText('Server');
    await expect(panel.locator('label[for="sp"]')).toHaveText('Port');
    await expect(panel.locator('label[for="tls"]')).toHaveText('TLS mode');
    await expect(panel.locator('label[for="su"]')).toHaveText('Username');
    await expect(panel.locator('label[for="spw"]')).toHaveText('Password');
    await expect(panel.locator('label[for="sf"]')).toHaveText('From address');
    await expect(panel.locator('label[for="st"]')).toHaveText('Send test mail to');
    await expect(panel.locator('#tls option')).toHaveText(['None', 'STARTTLS', 'SSL/TLS']);
    await expect(panel.locator('#spw')).toHaveAttribute('type', 'password');
    await expect(panel.locator('#spw')).toHaveValue('');
    await expect(panel.locator('#st')).toHaveValue('root@example.test');
    await expect(panel.getByRole('button', { name: 'Send test mail' })).toBeVisible();
    await expect(panel.locator('button[type="submit"]')).toHaveText('Save settings');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('keeps buttons and fields at a touch size on a phone', async ({ page }) => {
    await page.goto('/settings/smtp');
    if (!isWide(page)) {
      expect(await height(form(page).getByRole('button', { name: 'Send test mail' }))).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
      expect(await height(form(page).locator('button[type="submit"]'))).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
    }
    expect(await height(page.locator('#sh'))).toBeGreaterThanOrEqual(40);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('saving', () => {
  test('saves another port, shows it after a reload and puts it back', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/smtp');
    const original = await readForm(page);
    try {
      const mine = original.port === '2526' ? '2527' : '2526';
      await page.locator('#sp').fill(mine);
      await save(page);
      const saved = toast(form(page));
      await expect(saved).toContainText('SMTP settings saved');
      await expect(saved).toContainText('They apply to the next mail.');
      await expect(page.locator('#sp')).toHaveValue(mine);
      await page.reload();
      await expect(page.locator('#sp')).toHaveValue(mine);
      await save(page);
      await expect(toast(form(page))).toContainText('Nothing to save');
    } finally {
      await restore(page, original);
    }
    await page.goto('/settings/smtp');
    expect(await readForm(page)).toEqual(original);
  });

  test('refuses a bad port, host and address with the reason next to each field', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/smtp');
    const original = await readForm(page);
    await fillForm(page, {
      host: 'smtp://nope',
      port: '70000',
      tls: 'starttls',
      username: '',
      from: 'nobody',
    });
    await save(page);
    await expect(page.locator('#sh-e')).toHaveText(
      'Enter a host name or address, without a scheme, port or path.',
    );
    await expect(page.locator('#sp-e')).toHaveText('Enter a port from 1 to 65535.');
    await expect(page.locator('#sf-e')).toHaveText('Enter an address such as env-manager@example.com.');
    await expect(toast(form(page))).toHaveCount(0);
    await page.reload();
    expect(await readForm(page)).toEqual(original);
  });

  test('a stored password is never shown back and can be removed', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/smtp');
    const original = await readForm(page);
    const hadPassword = (await page.locator('input[name="remove_password"]').count()) > 0;
    try {
      if (!hadPassword) {
        await page.locator('#spw').fill(PASSWORD);
        await save(page);
        await expect(toast(form(page))).toContainText('SMTP settings saved');
      }
      await page.reload();
      await expect(page.locator('#spw')).toHaveValue('');
      await expect(page.locator('#spw')).toHaveAttribute('placeholder', '••••••••');
      await expect(page.locator('#spw-h')).toHaveText('Stored encrypted. Leave empty to keep it.');
      await expect(page.locator('input[name="remove_password"]')).toBeVisible();
      expect(await page.content()).not.toContain(PASSWORD);
      if (!hadPassword) {
        await page.locator('input[name="remove_password"]').check();
        await save(page);
        await expect(toast(form(page))).toContainText('SMTP settings saved');
        await expect(page.locator('input[name="remove_password"]')).toHaveCount(0);
        await expect(page.locator('#spw')).toHaveAttribute('placeholder', '');
      }
    } finally {
      if (!hadPassword) {
        await removeStoredPassword(page);
      }
      await restore(page, original);
    }
  });

  test('the audit entry of a save names a changed password and never shows it', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/smtp');
    const original = await readForm(page);
    const hadPassword = (await page.locator('input[name="remove_password"]').count()) > 0;
    try {
      await page.locator('#spw').fill(PASSWORD);
      await save(page);
      await expect(toast(form(page))).toContainText('SMTP settings saved');
      await page.goto('/audit?action=settings.update');
      const first = page.locator('#audit-results tr[data-fragment="audit-row"]').first();
      await expect(first).toContainText('settings.update');
      await expect(first.locator('xpath=following-sibling::tr[1]')).toContainText('smtp.password');
      expect(await page.content()).not.toContain(PASSWORD);
    } finally {
      if (!hadPassword) {
        await removeStoredPassword(page);
      }
      await restore(page, original);
    }
  });

  test('switching the server off raises the banner and saving it back clears it', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/smtp');
    const original = await readForm(page);
    const before = await banners(page).count();
    try {
      await fillForm(page, { ...original, host: '', from: '' });
      await save(page);
      await expect(toast(form(page))).toContainText('SMTP settings saved');
      await expect(banners(page)).toHaveCount(1);
      await expect(page.locator('#banner-area a[href="/settings/smtp"]')).toHaveText(
        'Open SMTP settings',
      );
      await page.goto('/projects');
      await expect(banners(page)).toHaveCount(1);
    } finally {
      await restore(page, original);
    }
    await expect(banners(page)).toHaveCount(before);
    await page.goto('/projects');
    await expect(banners(page)).toHaveCount(before);
  });
});

test.describe('the test mail', () => {
  test('reaches the mail catcher, readable and without any credential', async ({
    page,
    settingsLock: _lock,
  }, testInfo) => {
    await page.goto('/settings/smtp');
    const settings = await readForm(page);
    const address = `e2e-${testInfo.project.name}-test@example.test`;
    await page.locator('#st').fill(address);
    await form(page).getByRole('button', { name: 'Send test mail' }).click();
    const result = page.locator('#smtp-test');
    await expect(result.locator('[data-status="deployed"]')).toContainText('Test mail sent');
    const mail = await waitForMailTo(address, 'Test mail');
    expect(mail.subject).toBe('[ENV Manager] Test mail');
    const text = await plainText(mail.id);
    expect(text).toContain('Test mail');
    expect(text).toContain(`Server: ${settings.host}:${settings.port}`);
    expect(text).toContain('Sent by ENV Manager.');
    const markup = await htmlText(mail.id);
    expect(markup).toContain('<h1>Test mail</h1>');
    for (const body of [text, markup]) {
      expect(body).not.toContain(PASSWORD);
      expect(body).not.toContain('dev-only-password');
    }
  });

  test('says why when the server cannot be reached and refuses a bad address', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/smtp');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, host: '127.0.0.1', port: '1', tls: 'none' });
      await save(page);
      await expect(toast(form(page))).toContainText('SMTP settings saved');
      await form(page).getByRole('button', { name: 'Send test mail' }).click();
      const result = page.locator('#smtp-test');
      await expect(result.locator('[data-status="failed"]')).toHaveText('Test mail failed');
      await expect(result).toContainText('Could not connect to the mail server.');
      await page.locator('#st').fill('not-an-address');
      await form(page).getByRole('button', { name: 'Send test mail' }).click();
      await expect(result).toContainText('Enter a valid address to send the test mail to.');
    } finally {
      await restore(page, original);
    }
  });
});
