import { type Locator, type Page } from '@playwright/test';

import { stateFile } from '../global-setup';
import { csrfHeaders } from '../projects-api';
import { test } from '../settings-test';

const { expect } = test;
const WIDE_BREAKPOINT = 768;
const TOUCH_TARGET = 44;
const TOKEN = 'e2e-git-token-sentinel';
const PASSWORD_IN_ADDRESS = 'e2e-hunter2-sentinel';
const KEY_BODY = 'ZTJlLWdpdC1kZXBsb3kta2V5LXNlbnRpbmVs';
const DEPLOY_KEY = [
  '-----BEGIN OPENSSH PRIVATE KEY-----',
  KEY_BODY,
  'b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==',
  '-----END OPENSSH PRIVATE KEY-----',
].join('\n');
const UNREACHABLE_HTTPS = 'https://ops@127.0.0.1:1/ops/env.git';
const UNREACHABLE_SSH = 'ssh://git@127.0.0.1:1/ops/env.git';

/*
 * The Git tab on the development stack. Nothing here points the stack at a real repository, and a
 * spec that saves settings switches the hourly push off first, so the scheduler can never push or
 * raise the failure banner for the other specs. The states that need a server (a host key to
 * trust, a changed host key, a push that failed) are covered by pytest against a scripted runner
 * and by a real check against a throwaway SSH server. A spec holds the settings lock and puts back
 * what it found, credentials included.
 */

interface GitForm {
  url: string;
  branch: string;
  author: string;
  format: string;
  push: boolean;
}

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

function form(page: Page): Locator {
  return page.locator('#git');
}

function toast(scope: Locator): Locator {
  return scope.locator('[data-fragment="toast"]');
}

async function readForm(page: Page): Promise<GitForm> {
  return {
    url: await page.locator('#gu').inputValue(),
    branch: await page.locator('#gb').inputValue(),
    author: await page.locator('#ga').inputValue(),
    format: await page.locator('#gf').inputValue(),
    push: await page.locator('#gp').isChecked(),
  };
}

async function fillForm(page: Page, values: GitForm): Promise<void> {
  await page.locator('#gu').fill(values.url);
  await page.locator('#gb').fill(values.branch);
  await page.locator('#ga').fill(values.author);
  await page.locator('#gf').selectOption(values.format);
  await page.locator('#gp').setChecked(values.push);
}

async function save(page: Page): Promise<void> {
  await form(page).locator('button[type="submit"]').click();
}

/** Remove whatever credentials are stored. */
async function removeStoredCredentials(page: Page): Promise<void> {
  await page.goto('/settings/git');
  const key = page.locator('input[name="remove_deploy_key"]');
  const token = page.locator('input[name="remove_token"]');
  if ((await key.count()) + (await token.count()) === 0) {
    return;
  }
  if ((await key.count()) > 0) {
    await key.check();
  }
  if ((await token.count()) > 0) {
    await token.check();
  }
  await save(page);
  await expect(toast(form(page))).toBeVisible();
}

/** Put the settings back to what they were when the test began. */
async function restore(page: Page, original: GitForm): Promise<void> {
  await removeStoredCredentials(page);
  await page.goto('/settings/git');
  await fillForm(page, original);
  await save(page);
  await expect(toast(form(page))).toBeVisible();
}

test.describe('the tab', () => {
  test('shows the repository fields, the credentials, the push switch and the test row', async ({
    page,
  }) => {
    await page.goto('/settings/git');
    await expect(page).toHaveTitle('Settings · Git · ENV Manager');
    await expect(page.locator('nav[aria-label="Sections"] a[aria-current="page"]')).toHaveText(
      'Git',
    );
    const panel = page.locator('[data-fragment="settings-git"]');
    await expect(panel.locator('h2')).toHaveText('Private git repository');
    await expect(panel.locator('[role="note"]')).toContainText(
      'Pushes an encrypted export only, never plaintext.',
    );
    await expect(panel.locator('label[for="gu"]')).toHaveText('Repository URL');
    await expect(panel.locator('label[for="gb"]')).toHaveText('Branch');
    await expect(panel.locator('label[for="ga"]')).toHaveText('Commit author name');
    await expect(panel.locator('label[for="gf"]')).toHaveText('Export format');
    await expect(panel.locator('label[for="gk"]')).toHaveText('Deploy key (ssh addresses)');
    await expect(panel.locator('label[for="gt"]')).toHaveText('Access token (https addresses)');
    await expect(panel.locator('#gf option')).toHaveText([
      'One file per version',
      'Encrypted database snapshot',
    ]);
    await expect(panel.locator('#gk')).toHaveJSProperty('tagName', 'TEXTAREA');
    await expect(panel.locator('#gt')).toHaveAttribute('type', 'password');
    await expect(panel.locator('#gk')).toHaveValue('');
    await expect(panel.locator('#gt')).toHaveValue('');
    await expect(panel.locator('legend')).toHaveText('Push');
    await expect(panel.locator('label:has(#gp)')).toContainText(
      'Push hourly when something changed',
    );
    await expect(panel.getByRole('button', { name: 'Test connection' })).toBeVisible();
    await expect(panel.locator('button[type="submit"]')).toHaveText('Save settings');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('keeps buttons and fields at a touch size on a phone', async ({ page }) => {
    await page.goto('/settings/git');
    if (!isWide(page)) {
      expect(
        await height(form(page).getByRole('button', { name: 'Test connection' })),
      ).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect(await height(form(page).locator('button[type="submit"]'))).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
      expect(await height(form(page).locator('label:has(#gp)'))).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
    }
    expect(await height(page.locator('#gu'))).toBeGreaterThanOrEqual(40);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('saving', () => {
  test('saves another branch and format, shows them after a reload and puts them back', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      const mine: GitForm = {
        ...original,
        branch: original.branch === 'e2e-branch' ? 'e2e-other' : 'e2e-branch',
        format: 'snapshot',
        push: false,
      };
      await fillForm(page, mine);
      await save(page);
      const saved = toast(form(page));
      await expect(saved).toContainText('Git settings saved');
      await expect(saved).toContainText('The next scheduled push uses them.');
      await page.reload();
      expect(await readForm(page)).toEqual(mine);
      await save(page);
      await expect(toast(form(page))).toContainText('Nothing to save');
    } finally {
      await restore(page, original);
    }
    await page.goto('/settings/git');
    expect(await readForm(page)).toEqual(original);
  });

  test('refuses a password in the address, a bad branch, name, key and token', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    await fillForm(page, {
      url: `https://ops:${PASSWORD_IN_ADDRESS}@git.example.com/ops/env.git`,
      branch: 'bad branch',
      author: 'Ann <ann@example.com>',
      format: 'per_version',
      push: false,
    });
    await page.locator('#gk').fill('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 a public key');
    await page.locator('#gt').fill('a token with spaces');
    await save(page);
    await expect(page.locator('#gu-e')).toHaveText(
      'Do not put a password or token in the address. Use the credential field.',
    );
    await expect(page.locator('#gb-e')).toHaveText('Enter a branch name such as main.');
    await expect(page.locator('#ga-e')).toHaveText('Use printable characters, without < or >.');
    await expect(page.locator('#gk-e')).toHaveText('That is a public key. Paste the private key.');
    await expect(page.locator('#gt-e')).toHaveText(
      'Use printable characters without spaces, at most 1024.',
    );
    await expect(toast(form(page))).toHaveCount(0);
    expect(await page.content()).not.toContain(PASSWORD_IN_ADDRESS);
    await expect(page.locator('#gk')).toHaveValue('');
    await expect(page.locator('#gt')).toHaveValue('');
    await page.reload();
    expect(await readForm(page)).toEqual(original);
  });

  test('refuses an http, a local and an option-like address', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    for (const [address, message] of [
      ['http://git.example.com/ops/env.git', 'Use https. An access token is never sent over http.'],
      ['/srv/git/env.git', 'Use an ssh:// or https:// address, or the form git@host:path.'],
      ['--upload-pack=x', 'Use an ssh:// or https:// address, or the form git@host:path.'],
    ] as const) {
      await page.locator('#gu').fill(address);
      await save(page);
      await expect(page.locator('#gu-e')).toHaveText(message);
    }
    await page.reload();
    expect(await readForm(page)).toEqual(original);
  });

  test('stored credentials are never shown back, can be replaced by nothing and are removed', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, url: UNREACHABLE_HTTPS, push: false });
      await page.locator('#gt').fill(TOKEN);
      await page.locator('#gk').fill(DEPLOY_KEY);
      await save(page);
      await expect(toast(form(page))).toContainText('Git settings saved');
      await page.reload();
      await expect(page.locator('#gt')).toHaveValue('');
      await expect(page.locator('#gk')).toHaveValue('');
      await expect(page.locator('#gt')).toHaveAttribute('placeholder', '••••••••');
      await expect(page.locator('#gk')).toHaveAttribute('placeholder', '••••••••');
      await expect(page.locator('#gt-h')).toHaveText('Stored encrypted. Leave empty to keep it.');
      await expect(page.locator('#gk-h')).toHaveText('Stored encrypted. Leave empty to keep it.');
      await expect(page.locator('input[name="remove_token"]')).toBeVisible();
      await expect(page.locator('input[name="remove_deploy_key"]')).toBeVisible();
      const content = await page.content();
      expect(content).not.toContain(TOKEN);
      expect(content).not.toContain(KEY_BODY);
      await page.locator('input[name="remove_token"]').check();
      await save(page);
      await expect(toast(form(page))).toContainText('Git settings saved');
      await expect(page.locator('input[name="remove_token"]')).toHaveCount(0);
      await expect(page.locator('input[name="remove_deploy_key"]')).toBeVisible();
      await expect(page.locator('#gt')).toHaveAttribute('placeholder', '');
    } finally {
      await restore(page, original);
    }
  });

  test('the audit entry of a save names the credentials and the host, never their values', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, url: UNREACHABLE_HTTPS, push: false });
      await page.locator('#gt').fill(TOKEN);
      await save(page);
      await expect(toast(form(page))).toContainText('Git settings saved');
      await page.goto('/audit?action=settings.update');
      const rows = page.locator('#audit-results tr[data-fragment="audit-row"]');
      await expect(rows.first()).toContainText('settings.update');
      const text = (await rows.first().locator('xpath=following-sibling::tr[1]').textContent()) ?? '';
      expect(text).toContain('git.token');
      expect(text).not.toContain(TOKEN);
      expect(text).not.toContain('ops/env.git');
    } finally {
      await restore(page, original);
    }
  });
});

test.describe('the connection test', () => {
  test('says what is missing before it connects', async ({ page, settingsLock: _lock }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, url: '', push: false });
      await save(page);
      await expect(toast(form(page))).toBeVisible();
      const result = page.locator('#git-test');
      await form(page).getByRole('button', { name: 'Test connection' }).click();
      await expect(result.locator('[data-status="failed"]')).toHaveText('Connection failed');
      await expect(result).toContainText('No usable repository address is saved.');

      await fillForm(page, { ...original, url: UNREACHABLE_HTTPS, push: false });
      await save(page);
      await expect(toast(form(page))).toBeVisible();
      await form(page).getByRole('button', { name: 'Test connection' }).click();
      await expect(result).toContainText('No access token is stored for this https address.');
    } finally {
      await restore(page, original);
    }
  });

  test('reports a repository that cannot be reached without sending anything', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, url: UNREACHABLE_HTTPS, push: false });
      await page.locator('#gt').fill(TOKEN);
      await save(page);
      await expect(toast(form(page))).toContainText('Git settings saved');
      const result = page.locator('#git-test');
      await form(page).getByRole('button', { name: 'Test connection' }).click();
      await expect(result.locator('[data-status="failed"]')).toHaveText('Connection failed');
      await expect(result).toContainText('The repository could not be reached.');
      expect(await page.content()).not.toContain(TOKEN);

      await page.goto('/audit?action=git.test');
      const first = page.locator('#audit-results tr[data-fragment="audit-row"]').first();
      await expect(first).toContainText('git.test');
      const details =
        (await first.locator('xpath=following-sibling::tr[1]').textContent()) ?? '';
      expect(details).toContain('127.0.0.1:1');
      expect(details).not.toContain(TOKEN);
      expect(details).not.toContain('ops@');
    } finally {
      await restore(page, original);
    }
  });

  test('an ssh address with a deploy key says when its server cannot be reached', async ({
    page,
    settingsLock: _lock,
  }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, url: UNREACHABLE_SSH, push: false });
      await page.locator('#gk').fill(DEPLOY_KEY);
      await save(page);
      await expect(toast(form(page))).toContainText('Git settings saved');
      await expect(form(page)).toContainText('No host key is pinned for this address yet.');
      const result = page.locator('#git-test');
      await form(page).getByRole('button', { name: 'Test connection' }).click();
      await expect(result.locator('[data-status="failed"]')).toHaveText('Connection failed');
      await expect(result).toContainText('The repository could not be reached.');
      expect(await page.content()).not.toContain(KEY_BODY);
    } finally {
      await restore(page, original);
    }
  });

  test('an ssh address with no deploy key says so', async ({ page, settingsLock: _lock }) => {
    await page.goto('/settings/git');
    const original = await readForm(page);
    try {
      await fillForm(page, { ...original, url: UNREACHABLE_SSH, push: false });
      await save(page);
      await expect(toast(form(page))).toContainText('Git settings saved');
      await form(page).getByRole('button', { name: 'Test connection' }).click();
      await expect(page.locator('#git-test')).toContainText(
        'No deploy key is stored for this ssh address.',
      );
    } finally {
      await restore(page, original);
    }
  });
});

for (const role of ['admin', 'viewer', 'envScopedAdmin'] as const) {
  test.describe(`${role}`, () => {
    test.use({ storageState: stateFile(role) });

    test('is refused the tab, the save, the test and the pin', async ({ page }) => {
      const response = await page.goto('/settings/git');
      expect(response?.status()).toBe(403);
      await expect(page.locator('main')).toContainText('No access');
      for (const path of ['/settings/git', '/settings/git/test', '/settings/git/pin']) {
        const post = await page.request.post(path, {
          headers: { ...(await csrfHeaders(page.request)), 'HX-Request': 'true' },
          form: { url: 'https://git.example.com/ops/env.git', key_type: 'x', fingerprint: 'y' },
        });
        expect(post.status(), path).toBe(403);
      }
    });
  });
}
