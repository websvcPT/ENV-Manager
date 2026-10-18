import { chmodSync, existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Browser, Locator, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { stateFile } from '../global-setup';
import { createService, serviceId } from '../services-api';
import { expect, test } from '../tests/scratch-project';

/*
 * The deploy dialog against a usable Docker, on the demo stack: the real flow from pre-flight to
 * a post-deploy command that really recreates the workload, drift found on disk and forced over,
 * a scoped admin that deploys one file of a service, and a command that fails. The specs use the
 * seeded demo project (and a scratch project for the failing command) and put the target files
 * back to their baseline afterwards. Run by `make e2e-demo`; the demo override mounts the Docker
 * socket in the app and this container mounts the demo folder at its host path.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const TARGETS = join(STACK, 'environments');
const BASELINE = join(DEMO_ROOT, 'baseline', 'environments');
const DIST_SUFFIX = '-dist';
const DEPLOY_WAIT_MS = 90_000;

/** Put the target files back to the baseline, as `make demo-reset` does. */
function restoreBaseline(): void {
  for (const name of readdirSync(TARGETS)) {
    if (!name.endsWith(DIST_SUFFIX)) {
      rmSync(join(TARGETS, name), { recursive: true, force: true });
    }
  }
  for (const name of readdirSync(BASELINE)) {
    const target = join(TARGETS, name);
    writeFileSync(target, readFileSync(join(BASELINE, name)));
    chmodSync(target, 0o600);
  }
}

function mode(path: string): number {
  return statSync(path).mode & 0o777;
}

function dialog(page: Page): Locator {
  return page.locator('#deploy-dialog');
}

function row(page: Page, name: string): Locator {
  return dialog(page).locator(`[data-fragment="preflight-row"][data-env="${name}"]`);
}

function box(page: Page, name: string): Locator {
  return row(page, name).locator('input[type="checkbox"]');
}

function badge(page: Page, name: string): Locator {
  return row(page, name).locator('[data-status]');
}

function banner(page: Page): Locator {
  return dialog(page).locator('[data-fragment="deploy-result"]');
}

function submit(page: Page): Locator {
  return dialog(page).locator('[data-deploy-submit]');
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

async function openDemoDialog(browser: Browser, page: Page, name: string): Promise<number> {
  const service = await serviceId(browser, 'demo', 'stack', name);
  await page.goto(`/services/${service}`);
  await page.getByRole('button', { name: 'Deploy service' }).click();
  await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
  return service;
}

async function choose(page: Page, names: string[], all: string[]): Promise<void> {
  for (const name of all) {
    await box(page, name).setChecked(names.includes(name));
  }
}

test.beforeAll(() => {
  expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
});

test.beforeEach(() => restoreBaseline());
test.afterEach(() => restoreBaseline());

test.describe('superadmin on the demo stack', () => {
  test('deploys api from the dialog, creates the file and sees the post-deploy command run', async ({
    browser,
    page,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS + 30_000);
    await openDemoDialog(browser, page, 'api');

    await expect(dialog(page).locator('#dd-title')).toHaveText('Deploy api');
    await expect(badge(page, '.env.api')).toHaveAttribute('data-status', 'halt');
    await expect(row(page, '.env.api')).toContainText('Drift: Target changed outside the app');
    await expect(badge(page, 'demo-ca.pem')).toHaveAttribute('data-status', 'proceed');
    await expect(row(page, 'demo-ca.pem')).toContainText('Skipped, not env format');
    await expect(row(page, 'demo-ca.pem')).toContainText('Drift: Target missing');
    await choose(page, ['demo-ca.pem'], ['.env.api', 'demo-ca.pem']);

    await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
    await expect(box(page, 'demo-ca.pem')).toBeChecked();
    await expect(box(page, '.env.api')).not.toBeChecked();
    await expect(submit(page)).toHaveText('Deploy 1 environment');
    const preview = dialog(page).locator('[data-fragment="post-deploy-preview"]');
    await expect(preview).toContainText('The post-deploy command will run once');
    await expect(preview.locator('pre')).toHaveText(
      'docker compose up -d --no-deps --force-recreate {service_name}',
    );
    await expect(preview).toContainText('Command text is shown to the superadmin only.');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok', { timeout: DEPLOY_WAIT_MS });
    await expect(banner(page)).toContainText('1 environment written. The post-deploy command ran once.');
    const post = dialog(page).locator('[data-fragment="post-deploy-result"]');
    await expect(post.locator('[data-status="ran"]')).toHaveText('Ran');
    await expect(post).toContainText('Ran once · exit code 0');
    await expect(post.locator('pre')).toContainText('api');
    await expect(dialog(page).locator('[data-fragment="result-row"][data-env="demo-ca.pem"]')).toContainText(
      'Created environments/demo-ca.pem',
    );
    await expect(dialog(page).getByRole('link', { name: /^View audit entry #\d+$/ })).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    const created = join(TARGETS, 'demo-ca.pem');
    expect(existsSync(created)).toBe(true);
    expect(mode(created)).toBe(0o600);
    expect(readFileSync(created, 'utf8')).toContain('BEGIN CERTIFICATE');
    const driftedApi = readFileSync(join(BASELINE, '.env.api'), 'utf8');
    expect(readFileSync(join(TARGETS, '.env.api'), 'utf8')).toBe(driftedApi);

    await dialog(page).getByRole('button', { name: 'Close', exact: true }).last().click();
    await expect(page.locator('#postdeploy')).toContainText('exit code 0');
  });

  test('sees a file edited on disk as drift in the pre-flight and forces the overwrite', async ({
    browser,
    page,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS + 30_000);
    await openDemoDialog(browser, page, 'db');
    await expect(badge(page, '.env.db')).toHaveAttribute('data-status', 'unchanged');
    await expect(row(page, '.env.db')).toContainText('Drift: None');
    await expect(badge(page, '.env.db.secrets')).toHaveAttribute('data-status', 'block');
    await expect(row(page, '.env.db.secrets')).toContainText('1 missing key, policy: block');

    const managed = readFileSync(join(TARGETS, '.env.db'), 'utf8');
    writeFileSync(join(TARGETS, '.env.db'), `${managed}DEMO_DB_EDITED_ON_DISK=1\n`);
    await choose(page, ['.env.db'], ['.env.db', '.env.db.secrets']);
    await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
    await expect(badge(page, '.env.db')).toHaveAttribute('data-status', 'halt');
    await expect(row(page, '.env.db')).toContainText('Drift: Target changed outside the app');
    await expect(dialog(page).locator('[data-fragment="post-deploy-preview"]')).toContainText(
      'Not run because an environment would not be written.',
    );

    await dialog(page).getByText('Force overwrite').first().click();
    await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
    await expect(badge(page, '.env.db')).toHaveAttribute('data-status', 'warnout');
    await expect(dialog(page).locator('input[name="force"]')).toBeChecked();
    await submit(page).click();
    await expect(dialog(page).locator('#force-form')).toBeVisible();
    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toContain('DEMO_DB_EDITED_ON_DISK=1');

    await dialog(page).getByRole('button', { name: 'Force deploy 1 environment' }).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok', { timeout: DEPLOY_WAIT_MS });
    await expect(dialog(page).locator('[data-fragment="result-row"][data-env=".env.db"]')).toContainText(
      'previous file backed up, forced past the checks',
    );
    await expect(
      dialog(page).locator('[data-fragment="post-deploy-result"] [data-status="ran"]'),
    ).toBeVisible();
    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toBe(managed);
    const backups = readdirSync(TARGETS).filter((name) => name.includes('.env.db.bak-'));
    expect(backups).toHaveLength(1);
    expect(mode(join(TARGETS, backups[0]))).toBe(0o600);
    expect(readFileSync(join(TARGETS, backups[0]), 'utf8')).toContain('DEMO_DB_EDITED_ON_DISK=1');
  });

  test('a file whose dist keys are missing blocks its deploy and the result links the dist report', async ({
    browser,
    page,
  }) => {
    await openDemoDialog(browser, page, 'db');
    await choose(page, ['.env.db.secrets'], ['.env.db', '.env.db.secrets']);
    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'err');
    await expect(banner(page)).toContainText('Blocked: missing keys');
    await expect(banner(page)).toContainText(
      '.env.db.secrets is missing 1 key from its dist file. Policy is block. Nothing was written.',
    );
    await expect(dialog(page).locator('[data-fragment="result-row"]')).toContainText(
      'Missing DEMO_DB_BACKUP_PASSWORD',
    );
    await expect(dialog(page).getByRole('link', { name: 'Open dist report' })).toBeVisible();
    await expect(
      dialog(page).locator('[data-fragment="post-deploy-result"] [data-status="skipped"]'),
    ).toBeVisible();
  });
});

test.describe('scoped admin on the demo stack', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('deploys only the file in scope and the command runs once without showing its text', async ({
    browser,
    page,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS + 30_000);
    const secrets = join(TARGETS, '.env.db.secrets');
    const secretsBefore = readFileSync(secrets, 'utf8');
    const secretsStamp = statSync(secrets).mtimeMs;
    rmSync(join(TARGETS, '.env.db'));

    await openDemoDialog(browser, page, 'db');
    await expect(dialog(page).locator('[data-fragment="preflight-row"]')).toHaveCount(1);
    await expect(row(page, '.env.db')).toBeVisible();
    await expect(dialog(page)).not.toContainText('.env.db.secrets');
    await expect(badge(page, '.env.db')).toHaveAttribute('data-status', 'proceed');
    await box(page, '.env.db').check();
    await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
    const preview = dialog(page).locator('[data-fragment="post-deploy-preview"]');
    await expect(preview).toContainText(
      'The post-deploy command will run once, after this environment is written.',
    );
    await expect(preview).toContainText(
      'The service may also load environments you cannot see; they are not changed.',
    );
    await expect(preview.locator('pre')).toHaveCount(0);
    await expect(dialog(page)).not.toContainText('docker compose');

    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok', { timeout: DEPLOY_WAIT_MS });
    await expect(banner(page)).toContainText('1 environment written. The post-deploy command ran once.');
    await expect(dialog(page).locator('[data-fragment="post-deploy-result"] [data-status="ran"]')).toBeVisible();
    await expect(dialog(page)).not.toContainText('docker compose up');
    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toContain('DEMO_DB_ENGINE');
    expect(readFileSync(secrets, 'utf8')).toBe(secretsBefore);
    expect(statSync(secrets).mtimeMs).toBe(secretsStamp);
  });
});

test.describe('a failing command', () => {
  test('reads as deployed, post-deploy failed, with the exit code and the scrubbed output', async ({
    page,
    request,
    scratch,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS + 30_000);
    const deployment = await createDeployment(request, scratch.id, 'stack', STACK);
    const service = await createService(request, deployment, 'api');
    const name = `.env.e2e-${scratch.id}`;
    await createEnvironment(request, service, name, 'E2E_FAILING_COMMAND_VALUE=wxyz-9071\n');
    try {
      await page.goto(`/services/${service}`);
      const settings = page.locator('#postdeploy-settings');
      await settings.locator('#cmds').fill('docker compose ps --no-such-flag');
      await settings.getByRole('button', { name: 'Save settings' }).click();
      await expect(page.locator('[data-fragment="toast"]')).toContainText('Post-deploy settings saved');

      await page.getByRole('button', { name: 'Deploy service' }).click();
      await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
      await submit(page).click();
      await expect(banner(page)).toHaveAttribute('data-tone', 'warn', { timeout: DEPLOY_WAIT_MS });
      await expect(banner(page)).toContainText('Deployed, post-deploy failed');
      await expect(banner(page)).toContainText(
        /The post-deploy command exited with code \d+, so the workload may still run the old files\./,
      );
      const post = dialog(page).locator('[data-fragment="post-deploy-result"]');
      await expect(post.locator('[data-status="failed"]')).toHaveText('Failed');
      await expect(post).toContainText(/Failed · exit code \d+/);
      await expect(post.locator('pre')).toContainText(/unknown flag/i);
      await expect(dialog(page)).not.toContainText('wxyz-9071');
      expect(existsSync(join(TARGETS, name))).toBe(true);

      await dialog(page).getByRole('button', { name: 'Close', exact: true }).last().click();
      await expect(page.locator('#postdeploy [data-status="failed"]').first()).toBeVisible();
      await page.getByRole('button', { name: 'Deploy service' }).click();
      await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
      await box(page, name).check();
      await dialog(page).getByRole('button', { name: 'Run dry run again' }).click();
      await expect(
        dialog(page).locator('[data-fragment="post-deploy-preview"] [data-status="skipped"]'),
      ).toBeVisible();
      await expect(dialog(page).locator('[data-fragment="post-deploy-preview"]')).toContainText(
        'No file changed and the command runs only when one did.',
      );
    } finally {
      rmSync(join(TARGETS, name), { force: true });
    }
  });
});
