import { type Locator, type Page } from '@playwright/test';

import { caughtMail, htmlText, plainText, waitForMailTo } from '../mail-api';
import { test } from '../settings-test';
import { createUser, disableUser, uniqueUserName } from '../users-api';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';

const { expect } = test;
const SUPERADMIN = 'root@example.test';
const ROWS = [
  ['login_new_address', 'Login', true, false],
  ['lockout', 'Repeated failed logins or lockout', true, false],
  ['deploy_succeeded', 'Successful deploy', false, true],
  ['deploy_problem', 'Failed, blocked or halted deploy', true, true],
  ['file_changed', 'File changed', false, true],
  ['user_changed', 'User created, disabled, password changed or two-factor reset', true, false],
  ['backup_failed', 'Backup failure', true, false],
  ['git_push_failed', 'Git push failure', true, false],
  ['key_rotated', 'Master key rotated', true, false],
] as const;

/*
 * The Notifications tab on the development stack, whose SMTP settings point at the smtp4dev mail
 * catcher. A spec that saves the matrix holds the settings lock and puts back what it found; the
 * mail that an event sends is read back from the catcher.
 */

interface Matrix {
  enabled: Record<string, boolean>;
  recipients: Record<string, string>;
}

async function box(locator: Locator): Promise<{ width: number; height: number }> {
  const found = await locator.boundingBox();
  expect(found).not.toBeNull();
  return { width: found?.width ?? 0, height: found?.height ?? 0 };
}

function form(page: Page): Locator {
  return page.locator('#notifications');
}

function row(page: Page, kind: string): Locator {
  return form(page).locator(`tr[data-kind="${kind}"]`);
}

function toast(scope: Locator): Locator {
  return scope.locator('[data-fragment="toast"]');
}

async function readMatrix(page: Page): Promise<Matrix> {
  const matrix: Matrix = { enabled: {}, recipients: {} };
  for (const [kind, , , project] of ROWS) {
    matrix.enabled[kind] = await row(page, kind).locator('input[type="checkbox"]').isChecked();
    if (project) {
      matrix.recipients[kind] = await row(page, kind).locator('select').inputValue();
    }
  }
  return matrix;
}

async function writeMatrix(page: Page, matrix: Matrix): Promise<void> {
  for (const [kind, , , project] of ROWS) {
    await row(page, kind).locator('input[type="checkbox"]').setChecked(matrix.enabled[kind]);
    if (project) {
      await row(page, kind).locator('select').selectOption(matrix.recipients[kind]);
    }
  }
}

async function save(page: Page): Promise<void> {
  await form(page).locator('button[type="submit"]').click();
}

async function restore(page: Page, original: Matrix): Promise<void> {
  await page.goto('/settings/notifications');
  await writeMatrix(page, original);
  await save(page);
  await expect(toast(form(page))).toBeVisible();
}

test.describe('the tab', () => {
  test('lists the events with a switch each and the recipients of the project events', async ({
    page,
  }) => {
    await page.goto('/settings/notifications');
    await expect(page).toHaveTitle('Settings · Notifications · ENV Manager');
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'Notifications',
    );
    const panel = page.locator('[data-fragment="settings-notifications"]');
    await expect(panel.locator('h2')).toHaveText('Notification events');
    await expect(panel.locator('thead th')).toHaveText(['Event', 'Enabled', 'Recipients']);
    await expect(panel.locator('tbody tr')).toHaveCount(ROWS.length);
    for (const [kind, label, , project] of ROWS) {
      const line = row(page, kind);
      await expect(line.locator('th')).toContainText(label);
      await expect(line.locator('input[type="checkbox"]')).toHaveAttribute(
        'aria-label',
        `Enable ${label}`,
      );
      if (project) {
        await expect(line.locator('select')).toHaveAttribute(
          'aria-label',
          `Recipients for ${label}`,
        );
        await expect(line.locator('select option')).toHaveText([
          'Superadmins',
          'Project admins',
          'Superadmins and project admins',
        ]);
      } else {
        await expect(line.locator('select')).toHaveCount(0);
        await expect(line.locator('td').nth(1)).toHaveText('Superadmins');
      }
    }
    await expect(row(page, 'login_new_address').locator('th')).toContainText(
      'Only a sign-in from an address that user has not used before',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the table scrolls inside its panel on a phone and keeps touch sized controls', async ({
    page,
  }) => {
    await page.goto('/settings/notifications');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      const wrapper = form(page).locator('div.overflow-x-auto').first();
      expect(await wrapper.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
      const target = await box(row(page, 'deploy_problem').locator('label'));
      expect(target.width).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect(target.height).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect((await box(row(page, 'deploy_problem').locator('select'))).height).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
      expect((await box(form(page).locator('button[type="submit"]'))).height).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
    }
  });
});

test.describe('saving', () => {
  test('saves a switch and a recipients choice, shows them after a reload and puts them back', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/notifications');
    const original = await readMatrix(page);
    try {
      const changed: Matrix = {
        enabled: { ...original.enabled, file_changed: !original.enabled.file_changed },
        recipients: {
          ...original.recipients,
          deploy_succeeded: original.recipients.deploy_succeeded === 'both' ? 'superadmins' : 'both',
        },
      };
      await writeMatrix(page, changed);
      await save(page);
      const saved = toast(form(page));
      await expect(saved).toContainText('Notification settings saved');
      await expect(saved).toContainText('They apply to the next event.');
      await page.reload();
      expect(await readMatrix(page)).toEqual(changed);
      await save(page);
      await expect(toast(form(page))).toContainText('Nothing to save');
    } finally {
      await restore(page, original);
    }
    await page.goto('/settings/notifications');
    expect(await readMatrix(page)).toEqual(original);
  });

  test('the change is in the audit log', async ({ page, settingsLock: _lock }) => {
    await page.goto('/settings/notifications');
    const original = await readMatrix(page);
    try {
      await row(page, 'file_changed')
        .locator('input[type="checkbox"]')
        .setChecked(!original.enabled.file_changed);
      await save(page);
      await expect(toast(form(page))).toContainText('Notification settings saved');
      await page.goto('/audit?action=settings.update');
      const first = page.locator('#audit-results tr[data-fragment="audit-row"]').first();
      await expect(first.locator('xpath=following-sibling::tr[1]')).toContainText(
        'notifications.file_changed',
      );
    } finally {
      await restore(page, original);
    }
  });
});

test.describe('events', () => {
  test('a new user is announced to the superadmin and the mail never holds the password', async ({
    page,
    settingsLock: _lock,
  }, testInfo) => {
    const name = uniqueUserName(testInfo.project.name);
    await page.goto('/settings/notifications');
    const original = await readMatrix(page);
    const enabled = { ...original, enabled: { ...original.enabled, user_changed: true } };
    let userId: number | undefined;
    try {
      await writeMatrix(page, enabled);
      await save(page);
      await expect(toast(form(page))).toBeVisible();
      const created = await createUser(page.request, name, 'admin');
      userId = created.id;
      const mail = await waitForMailTo(SUPERADMIN, `User created: ${name}`);
      expect(mail.to).toEqual([SUPERADMIN]);
      const text = await plainText(mail.id);
      expect(text).toContain(`The account ${name} was created.`);
      expect(text).toContain('Group: admin');
      expect(text).toContain('Done by: root');
      const markup = await htmlText(mail.id);
      for (const body of [text, markup]) {
        expect(body).not.toContain(created.password);
      }
      await disableUser(page.request, created.id);
      await waitForMailTo(SUPERADMIN, `User disabled: ${name}`);
    } finally {
      if (userId !== undefined) {
        await disableUser(page.request, userId);
      }
      await restore(page, original);
    }
  });

  test('a switch that is off sends nothing', async ({ page, settingsLock: _lock }, testInfo) => {
    const name = uniqueUserName(testInfo.project.name);
    await page.goto('/settings/notifications');
    const original = await readMatrix(page);
    let userId: number | undefined;
    try {
      await writeMatrix(page, { ...original, enabled: { ...original.enabled, user_changed: false } });
      await save(page);
      await expect(toast(form(page))).toBeVisible();
      userId = (await createUser(page.request, name, 'viewer')).id;
      await disableUser(page.request, userId);
      await page.waitForTimeout(2000);
      expect((await caughtMail()).filter((mail) => mail.subject.includes(name))).toEqual([]);
    } finally {
      if (userId !== undefined) {
        await disableUser(page.request, userId);
      }
      await restore(page, original);
    }
  });
});

const WEBHOOK_SECRET = 'e2e-webhook-secret-sentinel-7731';
const REFUSED_URL = 'http://127.0.0.1:9/hooks/e2e-path-token-5520';

/*
 * The webhook controls. A spec that saves them holds the settings lock and puts the webhook back
 * to switched off with no address and no secret, which is how the development stack is found. The
 * webhook is never switched on here: the other specs deploy files, and every deploy would be posted
 * (and fail) to the address saved below, so the delivery of events is covered by the pytest suite
 * and the real receiver check, while the browser covers what a person sees and does.
 */
function secretField(page: Page): Locator {
  return page.locator('#ws');
}

async function resetWebhook(page: Page): Promise<void> {
  await page.goto('/settings/notifications');
  await page.locator('#wu').fill('');
  await page.locator('#we').setChecked(false);
  for (const name of ['remove_webhook_url', 'remove_webhook_secret']) {
    const remove = form(page).locator(`input[name="${name}"]`);
    if (await remove.count()) {
      await remove.setChecked(true);
    }
  }
  await secretField(page).fill('');
  await save(page);
  await expect(toast(form(page))).toBeVisible();
  await page.goto('/settings/notifications');
  await expect(page.locator('#wu')).toHaveValue('');
  await expect(page.locator('#wu')).toHaveAttribute('placeholder', '');
  await expect(page.locator('#we')).not.toBeChecked();
  await expect(secretField(page)).toHaveAttribute('placeholder', '');
}

test.describe('webhook', () => {
  test('shows the address, the secret, the switch and the test button', async ({ page }) => {
    await page.goto('/settings/notifications');
    const panel = form(page);
    await expect(panel.locator('label[for="wu"]')).toHaveText('Outbound webhook URL');
    await expect(panel.locator('label[for="ws"]')).toHaveText('Webhook shared secret');
    await expect(panel.locator('#wu')).toHaveAttribute('name', 'webhook_url');
    await expect(panel.locator('#ws')).toHaveAttribute('type', 'password');
    await expect(panel.locator('#ws')).toHaveAttribute('autocomplete', 'new-password');
    await expect(panel.locator('#we')).toHaveAttribute('name', 'webhook_enabled');
    await expect(panel.locator('label', { has: page.locator('#we') })).toContainText(
      'Dry runs are never sent.',
    );
    const testButton = panel.getByRole('button', { name: 'Send test event' });
    await expect(testButton).toHaveAttribute('hx-post', '/settings/notifications/webhook/test');
    await expect(testButton).toHaveAttribute('type', 'button');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      expect((await box(testButton)).height).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect((await box(page.locator('#wu'))).width).toBeGreaterThan(200);
    }
  });

  test('refuses another scheme, credentials, a short secret and a switch with nothing to send to', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/notifications');
    const alert = (id: string) => page.locator(`#${id}-e[role="alert"]`);
    await page.locator('#wu').fill('ftp://hooks.example.com/x');
    await secretField(page).fill(WEBHOOK_SECRET);
    await save(page);
    await expect(alert('wu')).toHaveText('Only http and https addresses are accepted.');
    await expect(page.locator('#wu')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#wu')).toHaveValue('');
    await expect(secretField(page)).toHaveValue('');

    await page.locator('#wu').fill('https://user:pass@hooks.example.com/x');
    await secretField(page).fill(WEBHOOK_SECRET);
    await save(page);
    await expect(alert('wu')).toContainText('Do not put a user name or password in the address');

    await page.locator('#wu').fill('https://hooks.example.com/x');
    await secretField(page).fill('short');
    await save(page);
    await expect(alert('ws')).toHaveText('Use at least 16 characters.');
    await expect(secretField(page)).toHaveValue('');

    await page.locator('#wu').fill('');
    await secretField(page).fill('');
    await page.locator('#we').setChecked(true);
    await save(page);
    await expect(alert('wu')).toHaveText('Enter the address to switch the webhook on.');
    await expect(alert('ws')).toHaveText('Enter a shared secret to switch the webhook on.');
    await page.goto('/settings/notifications');
    await expect(page.locator('#we')).not.toBeChecked();
  });

  test('stores the address and the secret without ever showing them, says why a test event failed and is audited', async ({
    page,
    settingsLock: _lock,
  }) => {
    await resetWebhook(page);
    try {
      await page.locator('#wu').fill(REFUSED_URL);
      await secretField(page).fill(WEBHOOK_SECRET);
      await save(page);
      await expect(toast(form(page))).toContainText('Notification settings saved');
      await expect(secretField(page)).toHaveValue('');
      await expect(secretField(page)).toHaveAttribute('placeholder', '••••••••');
      await expect(page.locator('#wu')).toHaveValue('');
      await expect(page.locator('#wu')).toHaveAttribute('placeholder', 'http://127.0.0.1:9/••••••••');
      await expect(form(page).locator('input[name="remove_webhook_url"]')).toBeVisible();
      await expect(form(page).locator('input[name="remove_webhook_secret"]')).toBeVisible();
      expect(await page.content()).not.toContain(WEBHOOK_SECRET);
      expect(await page.content()).not.toContain('e2e-path-token-5520');
      await page.reload();
      await expect(page.locator('#wu')).toHaveValue('');
      await expect(page.locator('#wu')).toHaveAttribute('placeholder', 'http://127.0.0.1:9/••••••••');
      await expect(page.locator('#we')).not.toBeChecked();
      expect(await page.content()).not.toContain(WEBHOOK_SECRET);
      expect(await page.content()).not.toContain('e2e-path-token-5520');

      await form(page).getByRole('button', { name: 'Send test event' }).click();
      const result = page.locator('#webhook-test');
      await expect(result).toContainText('Test event failed');
      await expect(result).toContainText('The webhook could not be reached.');
      await expect(result).not.toContainText('127.0.0.1');
      await expect(page.locator('#banner-area')).not.toContainText('deploy webhook');

      await page.goto('/audit?action=webhook.test');
      const entry = page.locator('#audit-results tr[data-fragment="audit-row"]').first();
      await expect(entry).toContainText('webhook.test');
      await expect(entry.locator('[data-status="failed"]')).toBeVisible();
      await expect(entry.locator('xpath=following-sibling::tr[1]')).toContainText(
        'The webhook could not be reached.',
      );
      await page.goto('/audit?action=settings.update');
      const first = page.locator('#audit-results tr[data-fragment="audit-row"]').first();
      const detail = first.locator('xpath=following-sibling::tr[1]');
      await expect(detail).toContainText('notifications.webhook_secret');
      await expect(page.locator('#audit-results')).toContainText('notifications.webhook_url');
      expect(await page.content()).not.toContain(WEBHOOK_SECRET);
      expect(await page.content()).not.toContain('e2e-path-token-5520');
    } finally {
      await resetWebhook(page);
    }
  });
});
