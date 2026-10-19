import {
  chmodSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';

import type { APIRequestContext, Locator, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { saveChangedCommands } from '../post-deploy-form';
import { createService } from '../services-api';
import { expect, test } from '../tests/scratch-project';

/*
 * The drift view on the demo stack with a usable Docker: a file edited on disk halts its deploy,
 * the result links the drift view, importing the target as a version makes the next deploy find
 * no drift, and overwriting it (after the confirmation) leaves a 0600 backup and really recreates
 * the workload. The specs use a deployment of a throwaway project rooted at the demo stack, so the
 * seeded demo files keep their versions and history, and put the target files back to their
 * baseline afterwards. Run by `make e2e-demo`.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const TARGETS = join(STACK, 'environments');
const BASELINE = join(DEMO_ROOT, 'baseline', 'environments');
const DIST_SUFFIX = '-dist';
const DEPLOY_WAIT_MS = 90_000;
const EDITED_LINE = 'DEMO_DB_EDITED_ON_DISK=1\n';
const COMMAND = 'docker compose up -d --no-deps --force-recreate db';

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

function banner(page: Page): Locator {
  return dialog(page).locator('[data-fragment="deploy-result"]');
}

function submit(page: Page): Locator {
  return dialog(page).locator('[data-deploy-submit]');
}

function header(page: Page): Locator {
  return page.locator('[data-fragment="page-header"]');
}

function badge(page: Page): Locator {
  return header(page).locator('[data-status]');
}

function toast(page: Page): Locator {
  return page.locator('[data-fragment="toast"]');
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

interface Stack {
  service: number;
  environment: number;
}

/**
 * A `db` service of a throwaway project, rooted at the demo stack, with the recreate command
 * stored and a `.env.db` that holds exactly what the baseline target holds.
 */
async function stack(page: Page, request: APIRequestContext, projectId: number): Promise<Stack> {
  expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
  const deployment = await createDeployment(request, projectId, 'stack', STACK);
  const service = await createService(request, deployment, 'db');
  const environment = await createEnvironment(
    request,
    service,
    '.env.db',
    readFileSync(join(BASELINE, '.env.db'), 'utf8'),
  );
  await page.goto(`/services/${service}`);
  const form = page.locator('#postdeploy-settings');
  await form.locator('#cmds').fill(COMMAND);
  await saveChangedCommands(form);
  await expect(toast(page)).toContainText('Post-deploy settings saved');
  return { service, environment };
}

function fileBox(page: Page): Locator {
  return dialog(page).locator(
    '[data-fragment="preflight-row"][data-env=".env.db"] input[type="checkbox"]',
  );
}

async function openDialog(page: Page, service: number): Promise<void> {
  await page.goto(`/services/${service}`);
  await page.getByRole('button', { name: 'Deploy service' }).click();
  await expect(dialog(page).locator('[data-fragment="preflight-table"]')).toBeVisible();
}

/**
 * Deploy the file once (the target already matches, so it is found identical and the app records
 * it as written), edit the target on disk and deploy again: the Halted result. Returns the
 * edited content.
 */
async function halted(page: Page, service: number): Promise<string> {
  await openDialog(page, service);
  await fileBox(page).check();
  await submit(page).click();
  await expect(banner(page)).toHaveAttribute('data-tone', 'ok', { timeout: DEPLOY_WAIT_MS });

  const edited = readFileSync(join(TARGETS, '.env.db'), 'utf8') + EDITED_LINE;
  writeFileSync(join(TARGETS, '.env.db'), edited);
  await openDialog(page, service);
  await fileBox(page).check();
  await submit(page).click();
  await expect(banner(page)).toHaveAttribute('data-tone', 'warn', { timeout: DEPLOY_WAIT_MS });
  await expect(banner(page)).toContainText('Halted: drift');
  return edited;
}

test.beforeEach(() => restoreBaseline());
test.afterEach(() => restoreBaseline());

test.describe('superadmin on the demo stack', () => {
  test('opens the drift view from the Halted result, imports the target and deploys with no drift', async ({
    page,
    request,
    scratch,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS * 2 + 60_000);
    const { service, environment } = await stack(page, request, scratch.id);
    const edited = await halted(page, service);

    await dialog(page).getByRole('link', { name: 'Open drift view' }).click();
    await page.waitForURL(`**/environments/${environment}/drift`);
    await expect(badge(page)).toHaveText('Drift');
    const line = page.locator('[data-fragment="drift-row"]', { hasText: 'DEMO_DB_EDITED_ON_DISK' });
    await expect(line.locator('[data-change="disk-added"]')).toHaveText('Added on disk');
    await expect(line).toContainText('not set');
    const content = await page.content();
    expect(content).not.toContain('demo-sqlite');
    expect(content).not.toContain(DEMO_ROOT);
    await page.locator('button[hx-post*="/drift/reveal"]').click();
    await expect(page.locator('#drift-target-0 [data-reveal-value]')).toHaveText('1');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.getByRole('button', { name: 'Import target as new version' }).click();
    await page.waitForURL(/drift\?imported=2$/);
    await expect(toast(page)).toContainText('Imported the target as version 2. Pending deploy.');
    await expect(badge(page)).toHaveText('No drift');
    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toBe(edited);

    await openDialog(page, service);
    const row = dialog(page).locator('[data-fragment="preflight-row"][data-env=".env.db"]');
    await expect(row.locator('[data-status]')).toHaveAttribute('data-status', 'unchanged');
    await expect(row).toContainText('Drift: None');
    await fileBox(page).check();
    await submit(page).click();
    await expect(banner(page)).toHaveAttribute('data-tone', 'ok', { timeout: DEPLOY_WAIT_MS });
    await expect(dialog(page).locator('[data-fragment="result-row"]')).toContainText(
      'Already matches this version',
    );
    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toBe(edited);
    expect(readdirSync(TARGETS).filter((name) => name.includes('.bak-'))).toEqual([]);
  });

  test('overwrites the target after the confirmation, keeps a 0600 backup and recreates the workload', async ({
    page,
    request,
    scratch,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS * 3 + 60_000);
    const { service, environment } = await stack(page, request, scratch.id);
    const edited = await halted(page, service);
    const managed = readFileSync(join(BASELINE, '.env.db'), 'utf8');

    await page.goto(`/environments/${environment}/drift`);
    await expect(badge(page)).toHaveText('Drift');
    await page.getByRole('button', { name: /^Overwrite target/ }).click();
    const confirm = page.locator('#overwrite-dialog');
    await expect(confirm).toBeVisible();
    await expect(confirm).toContainText('managed version v1');
    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toBe(edited);
    await confirm.getByRole('button', { name: 'Overwrite target' }).click();
    await page.waitForURL(/drift\?overwritten=1&backup=1&same=0&post=ok$/, {
      timeout: DEPLOY_WAIT_MS,
    });
    await expect(toast(page)).toContainText('Overwrote the target with version 1.');
    await expect(toast(page)).toContainText('The previous file was kept as a backup next to it.');
    await expect(badge(page)).toHaveText('No drift');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    expect(readFileSync(join(TARGETS, '.env.db'), 'utf8')).toBe(managed);
    expect(mode(join(TARGETS, '.env.db'))).toBe(0o600);
    const backups = readdirSync(TARGETS).filter((name) => name.includes('.env.db.bak-'));
    expect(backups).toHaveLength(1);
    expect(mode(join(TARGETS, backups[0]))).toBe(0o600);
    expect(readFileSync(join(TARGETS, backups[0]), 'utf8')).toBe(edited);

    await page.goto(`/services/${service}`);
    await expect(page.locator('#postdeploy')).toContainText('exit code 0');
  });
});
