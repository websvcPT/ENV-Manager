import type { Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { saveChangedCommands } from '../post-deploy-form';
import { createService } from '../services-api';
import { expect, test } from '../tests/scratch-project';

/*
 * The post-deploy command settings against a usable Docker: the live status line, the enabled
 * form, a stored save, refusals and the compose check, which really runs `docker compose config`
 * on the host daemon's behalf. Run by `make e2e-demo` (the demo override mounts the socket and
 * the demo folder at its host path).
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const NO_COMPOSE_FILE = '/srv/deployments/sample-a';

function panel(page: Page) {
  return page.locator('#postdeploy');
}

function settings(page: Page) {
  return page.locator('#postdeploy-settings');
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/** A service in a deployment rooted at the demo Compose stack, inside the throwaway project. */
async function stackService(
  request: Parameters<typeof createDeployment>[0],
  projectId: number,
): Promise<number> {
  expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
  const deployment = await createDeployment(request, projectId, 'stack', STACK);
  return createService(request, deployment, 'api');
}

test.describe('superadmin with a usable Docker', () => {
  test('sees the live status and an enabled form with the defaults', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);

    const status = panel(page).locator('[data-fragment="docker-status"]');
    await expect(status).toContainText('Docker socket mounted');
    await expect(status).toContainText(/daemon reachable · compose v\d+\.\d+/);
    await expect(status.locator('[data-status="deployed"]')).toBeVisible();
    await expect(status.getByRole('link')).toHaveCount(0);

    const form = settings(page);
    await expect(form.locator('[role="note"]')).toHaveCount(0);
    await expect(form.locator('#wd')).toBeEnabled();
    await expect(form.locator('#wd')).toHaveValue('');
    await expect(form.locator('#wd')).toHaveAttribute('placeholder', STACK);
    await expect(form.locator('#cmds')).toBeEnabled();
    await expect(form.locator('#cmds')).toHaveValue('');
    await expect(form.locator('#timeout')).toHaveValue('120');
    await expect(form.locator('input[name="run_only_if_changed"]')).toBeChecked();
    await expect(form.getByRole('button', { name: 'Test command' })).toBeEnabled();
    await expect(form.getByRole('button', { name: 'Save settings' })).toBeEnabled();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('saves the settings, shows a toast and keeps them after a reload', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    const form = settings(page);
    await form.locator('#cmds').fill('docker compose up -d --no-deps api\n\ndocker ps');
    await form.locator('#timeout').fill('300');
    await form.locator('input[name="run_only_if_changed"]').uncheck();
    await form.getByRole('button', { name: 'Save settings' }).click();

    const review = form.locator('#postdeploy-review');
    await expect(review).toBeVisible();
    await expect(review).toContainText('Nothing is stored until you confirm.');
    await expect(review.locator('li')).toHaveCount(2);
    await expect(review.locator('li').first().locator('code')).toHaveText([
      'docker',
      'compose',
      'up',
      '-d',
      '--no-deps',
      'api',
    ]);
    await expect(review.locator('li').last().locator('code')).toHaveText(['docker', 'ps']);
    await expect(form.getByRole('button', { name: 'Save settings' })).toHaveCount(0);
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);
    await page.reload();
    await expect(form.locator('#cmds')).toHaveValue('');

    await form.locator('#cmds').fill('docker compose up -d --no-deps api\n\ndocker ps');
    await form.locator('#timeout').fill('300');
    await form.locator('input[name="run_only_if_changed"]').uncheck();
    await saveChangedCommands(form);

    const toast = page.locator('[data-fragment="toast"]');
    await expect(toast).toContainText('Post-deploy settings saved');
    await expect(toast).toContainText('They apply from the next deploy.');
    await expect(form.locator('#cmds')).toHaveValue(
      'docker compose up -d --no-deps api\ndocker ps',
    );
    await expect(form.locator('p[role="alert"]')).toHaveCount(0);

    await page.reload();
    await expect(form.locator('#cmds')).toHaveValue(
      'docker compose up -d --no-deps api\ndocker ps',
    );
    await expect(form.locator('#timeout')).toHaveValue('300');
    await expect(form.locator('input[name="run_only_if_changed"]')).not.toBeChecked();
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a refused save shows every reason next to its field, keeps what was typed and stores nothing', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    const form = settings(page);
    await form.locator('#wd').fill('/etc');
    await form.locator('#cmds').fill('ls -la\ndocker ps | cat');
    await form.locator('#timeout').fill('0');
    await form.getByRole('button', { name: 'Save settings' }).click();

    await expect(form.locator('#wd-e')).toHaveText('The working directory is outside the allowed paths.');
    await expect(form.locator('#cmds-e')).toHaveText('Line 1: the first word must be docker.');
    await expect(form.locator('#timeout-e')).toHaveText(
      'Enter a whole number of seconds from 1 to 3600.',
    );
    await expect(form.locator('#wd')).toHaveValue('/etc');
    await expect(form.locator('#cmds')).toHaveValue('ls -la\ndocker ps | cat');
    await expect(form.locator('#timeout')).toHaveValue('0');
    await expect(form.locator('#wd')).toHaveAttribute('aria-invalid', 'true');
    await expect(form.locator('#cmds')).toHaveAttribute('aria-describedby', 'cmds-e');
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.reload();
    await expect(form.locator('#cmds')).toHaveValue('');
    await expect(form.locator('#timeout')).toHaveValue('120');
  });

  test('a refusal clears once the settings are corrected and saved', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    const form = settings(page);
    await form.locator('#cmds').fill('rm -rf');
    await form.getByRole('button', { name: 'Save settings' }).click();
    await expect(form.locator('#cmds-e')).toBeVisible();

    await form.locator('#cmds').fill('docker compose ps');
    await saveChangedCommands(form);
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Post-deploy settings saved');
    await expect(form.locator('#cmds-e')).toHaveCount(0);
    await expect(form.locator('#cmds')).toHaveValue('docker compose ps');
  });

  test('the safety net refuses the commands that remove a project or data, with the rule', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    const form = settings(page);
    const refused: Array<[string, string]> = [
      ['docker compose down', 'Line 1: docker compose down is not allowed.'],
      ['docker ps\ndocker system prune -af', 'Line 2: prune commands are not allowed.'],
      ['docker compose rm -sfv api', 'Line 1: the -v and --volumes options of rm are not allowed'],
      ['docker ps\n'.repeat(11), 'Use at most 10 commands.'],
    ];
    for (const [commands, reason] of refused) {
      await form.locator('#cmds').fill(commands);
      await form.getByRole('button', { name: 'Save settings' }).click();
      await expect(form.locator('#cmds-e')).toContainText(reason);
      await expect(form.locator('#postdeploy-review')).toHaveCount(0);
      await expect(form.locator('#cmds')).toHaveValue(commands);
    }
    await page.reload();
    await expect(form.locator('#cmds')).toHaveValue('');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('commands edited after the review are shown again before anything is stored', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    const form = settings(page);
    await form.locator('#cmds').fill('docker compose ps');
    await form.getByRole('button', { name: 'Save settings' }).click();
    await expect(form.locator('#postdeploy-review li')).toHaveCount(1);

    await form.locator('#cmds').fill("docker run --name 'a b' alpine\ndocker ps");
    await form.getByRole('button', { name: 'Confirm and save' }).click();
    const review = form.locator('#postdeploy-review');
    await expect(review.locator('li')).toHaveCount(2);
    await expect(review.locator('li').first().locator('code')).toHaveText([
      'docker',
      'run',
      '--name',
      'a b',
      'alpine',
    ]);
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);

    await form.getByRole('button', { name: 'Confirm and save' }).click();
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Post-deploy settings saved');
    await expect(form.locator('#postdeploy-review')).toHaveCount(0);
    await expect(form.locator('#cmds')).toHaveValue("docker run --name 'a b' alpine\ndocker ps");
  });

  test('Test command checks the Compose file of the deployment root', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    const result = settings(page).locator('#test-result');
    await expect(result).toContainText('checks the path and the Compose file.');
    await settings(page).getByRole('button', { name: 'Test command' }).click();

    await expect(result).toContainText('The working directory and the Compose file are valid.');
    await expect(result.locator('[role="note"]')).toBeVisible();
    await expect(result.locator('pre')).toHaveCount(0);
    await expect(settings(page).locator('#cmds')).toHaveValue('');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('Test command checks the typed folder and reports a folder without a Compose file', async ({
    page,
    request,
    scratch,
  }) => {
    const service = await stackService(request, scratch.id);
    await page.goto(`/services/${service}`);
    await settings(page).locator('#wd').fill(NO_COMPOSE_FILE);
    await settings(page).getByRole('button', { name: 'Test command' }).click();

    const result = settings(page).locator('#test-result');
    await expect(result).toContainText('Docker Compose reported a problem with the working directory.');
    await expect(result.locator('pre')).toContainText(/no configuration file provided/i);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await settings(page).locator('#wd').fill('/etc');
    await settings(page).getByRole('button', { name: 'Test command' }).click();
    await expect(result).toContainText('The working directory is outside the allowed paths.');
    await expect(result.locator('pre')).toHaveCount(0);

    await settings(page).locator('#wd').fill('');
    await settings(page).getByRole('button', { name: 'Test command' }).click();
    await expect(result).toContainText('The working directory and the Compose file are valid.');
  });
});
