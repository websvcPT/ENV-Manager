import { type Locator, type Page } from '@playwright/test';

import { caughtMail, htmlText, plainText, waitForMailTo } from '../mail-api';
import { test } from '../settings-test';
import { createUser, disableUser, uniqueUserName } from '../users-api';

const { expect } = test;
const WIDE_BREAKPOINT = 768;
const TOUCH_TARGET = 44;
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

function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
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
