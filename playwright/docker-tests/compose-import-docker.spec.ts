import { rmSync, writeFileSync } from 'node:fs';

import type { Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { expect, test } from '../tests/scratch-project';
import { horizontalOverflow } from '../viewport';

/*
 * The Docker Compose import with a usable Docker: the post-deploy commands it offers, unticked
 * and editable, and the ones ticked stored on the services and on the deployment files. The
 * demo stack has no deployment .env, so the spec writes one beside its Compose file for the time
 * of the test (Compose reads it, the stack uses no variable from it); the import only reads the
 * stack and links its Compose file. Run by `make e2e-demo` (the demo override mounts the
 * socket and the demo folder at its host path).
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const UP = 'docker compose -f compose.yaml up -d';
const FILES_NAME = 'Deployment files';
const STACK_ENV = `${STACK}/.env`;

function run(page: Page, owner: string) {
  return page.locator(`input[name="run:${owner}"]`);
}

function command(page: Page, owner: string) {
  return page.locator(`input[name="command:${owner}"]`);
}

test.describe('import from Docker Compose with a usable Docker', () => {
  test.beforeEach(() => {
    writeFileSync(STACK_ENV, 'DEMO_STACK_NOTE=from-the-docker-spec\n');
  });

  test.afterEach(() => {
    rmSync(STACK_ENV, { force: true });
  });

  test('offers a command per service and for the deployment files and stores the ticked ones', async ({
    page,
    request,
    scratch,
  }) => {
    expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
    const id = await createDeployment(request, scratch.id, 'stack', STACK);
    await page.goto(`/deployments/${id}/import-compose`);
    await page.getByRole('button', { name: 'Scan the files' }).click();
    await expect(page.locator('[data-fragment="import-preview"]')).toBeVisible();
    await expect(page.locator('[data-fragment="import-no-commands"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    for (const name of ['api', 'db']) {
      await expect(command(page, name)).toHaveValue(`${UP} --no-deps ${name}`);
      await expect(run(page, name)).not.toBeChecked();
    }
    await expect(command(page, FILES_NAME)).toHaveValue(UP);
    await expect(run(page, FILES_NAME)).not.toBeChecked();

    // Unticking a service unticks and disables its command; ticking it again enables it.
    const db = page.locator(
      '[data-fragment="import-service-row"][data-service="db"] input[name="service"]',
    );
    await run(page, 'db').check();
    await db.uncheck();
    await expect(run(page, 'db')).toBeDisabled();
    await expect(run(page, 'db')).not.toBeChecked();
    await db.check();
    await expect(run(page, 'db')).toBeEnabled();

    await run(page, 'api').check();
    await command(page, 'api').fill(`${UP} --no-deps --force-recreate api`);
    await run(page, FILES_NAME).check();
    await page.getByRole('button', { name: 'Import selected…' }).click();

    const dialog = page.locator('#import-confirm');
    await expect(dialog.locator('[data-import-count="commands"]')).toHaveText('2');
    const api = dialog.locator('[data-import-command="api"] code');
    await expect(api).toHaveText([
      'docker',
      'compose',
      '-f',
      'compose.yaml',
      'up',
      '-d',
      '--no-deps',
      '--force-recreate',
      'api',
    ]);
    await expect(dialog.locator(`[data-import-command="${FILES_NAME}"] code`)).toHaveCount(6);
    await dialog.getByRole('button', { name: 'Import', exact: true }).click();

    await expect(page).toHaveURL(new RegExp(`/deployments/${id}/files\\?imported=2,4,4,0,0,2$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText(
      '2 post-deploy commands set.',
    );
    const filesRow = page.locator('[data-fragment="deployment-files-row"]');
    await expect(filesRow).toContainText('1 file');
    await filesRow.locator('a').first().click();
    await expect(page.locator('#postdeploy-settings #cmds')).toHaveValue(UP);
    await page.goto(`/deployments/${id}/settings`);
    await expect(
      page.locator('[data-fragment="compose-file-row"][data-file="compose.yaml"]'),
    ).toBeVisible();
    await page.goto(`/deployments/${id}/files`);
    await page.locator('#services [data-fragment="service-row"] th a', { hasText: 'api' }).click();
    await expect(page.locator('#postdeploy-settings #cmds')).toHaveValue(
      `${UP} --no-deps --force-recreate api`,
    );
  });
});
