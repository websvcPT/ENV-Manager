import { rmSync } from 'node:fs';
import { join } from 'node:path';

import type { Locator, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { csrfHeaders } from '../http';
import { createService } from '../services-api';
import { rowAction } from '../tests/row-actions';
import { expect, test } from '../tests/scratch-project';
import { horizontalOverflow, isWide } from '../viewport';

/*
 * Start, stop, restart and recreate the containers of the demo stack from the Overview of a
 * scratch deployment that points at it, with the real daemon. The first spec runs on the
 * project alone (no Compose file linked), the second on the linked Compose file. `make e2e-demo`
 * starts the demo workloads before it runs these specs, and each spec leaves them running.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const TARGETS = join(STACK, 'environments');
const DEMO_PROJECT = 'env-manager-demo';
const SETTLE_MS = 30_000;

function runtime(page: Page): Locator {
  return page.locator('#runtime');
}

/** The rows of the services table or the items of the phone list, whichever the layout shows. */
function rowOf(page: Page, service: string): Locator {
  return page.locator(
    `[data-fragment="service-summary-row"][data-service="${service}"]:visible, ` +
      `[data-fragment="service-summary-item"][data-service="${service}"]:visible`,
  );
}

function dialog(page: Page): Locator {
  return page.locator('#container-action');
}

function toast(page: Page): Locator {
  return page.locator('#container-toast [data-fragment="toast"]');
}

function startedAt(page: Page, service: string): Promise<string | null> {
  return rowOf(page, service).locator('[data-uptime] time').first().getAttribute('datetime');
}

/** Choose an action of a service row and wait for its confirmation. */
async function choose(page: Page, service: string, key: string): Promise<void> {
  const control = await rowAction(rowOf(page, service), key);
  await control.click();
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).locator('[data-fragment="container-action"]')).toHaveAttribute(
    'data-action',
    key,
  );
}

/** Confirm the open dialog: it closes and the toast of the answer shows. */
async function confirm(page: Page): Promise<void> {
  await dialog(page).locator('button[type="submit"]').click();
  await expect(dialog(page)).toBeHidden({ timeout: SETTLE_MS });
  await expect(toast(page)).toBeVisible();
}

async function command(page: Page): Promise<string> {
  const chips = await dialog(page).locator('[data-fragment="container-command"] code').allTextContents();
  return chips.join(' ');
}

test.beforeAll(() => {
  expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
});

test.describe('Container actions with a usable Docker', () => {
  test('restart changes the uptime, stop then start brings the container back', async ({
    page,
    request,
    scratch,
  }) => {
    test.setTimeout(4 * SETTLE_MS);
    const id = await createDeployment(request, scratch.id, 'stack', STACK, 'block', DEMO_PROJECT);
    try {
      await page.goto(`/deployments/${id}`);
      await expect(runtime(page)).toHaveAttribute('aria-busy', 'false');
      const api = rowOf(page, 'api');
      await expect(api.locator('[data-status="up"]')).toBeVisible();
      await expect(api.locator('[data-uptime]')).toContainText(/Up \d+ (second|minute|hour|day)s?/);
      await expect(page.locator('[data-row-action="recreate"]')).toHaveCount(0);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
      const first = await startedAt(page, 'api');
      expect(first).not.toBeNull();

      await choose(page, 'api', 'restart');
      await expect(dialog(page).locator('h2')).toHaveText('Restart api?');
      await expect(dialog(page)).toContainText('stops and starts again with the configuration it already has');
      expect(await command(page)).toBe(`docker compose -p ${DEMO_PROJECT} restart api`);
      await expect(dialog(page).locator('button[type="submit"]')).toHaveText('Restart api');
      await page.evaluate(() => {
        const seen: Array<{ source: string; timeout: number }> = [];
        (window as unknown as { requests: typeof seen }).requests = seen;
        document.addEventListener('htmx:before:request', (event) => {
          const detail = (event as CustomEvent).detail as {
            ctx?: { sourceElement?: Element; request?: { timeout?: unknown } };
          };
          seen.push({
            source: detail.ctx?.sourceElement?.id ?? '',
            timeout: Number(detail.ctx?.request?.timeout ?? 0),
          });
        });
      });
      await confirm(page);
      await expect(toast(page)).toContainText('api restarted');
      await expect(toast(page)).toHaveAttribute('role', 'status');
      const requests = await page.evaluate(
        () => (window as unknown as { requests: Array<{ source: string; timeout: number }> }).requests,
      );
      const posted = requests.filter((entry) => entry.source === 'container-action-form');
      expect(posted).toHaveLength(1);
      expect(posted[0].timeout).toBeGreaterThan(180_000);
      await expect.poll(() => startedAt(page, 'api'), { timeout: SETTLE_MS }).not.toBe(first);
      await expect(api.locator('[data-status="up"]')).toBeVisible();

      await choose(page, 'api', 'stop');
      await expect(dialog(page).locator('h2')).toHaveText('Stop api?');
      await expect(dialog(page)).toContainText('stays stopped until someone starts it');
      await expect(dialog(page).locator('button[type="submit"]')).toHaveClass(/border-danger/);
      await confirm(page);
      await expect(toast(page)).toContainText('api stopped');
      await expect(api.locator('[data-status="exited"]')).toBeVisible({ timeout: SETTLE_MS });
      await expect(api.locator('[data-status="up"]')).toHaveCount(0);
      await expect(page.locator('[data-fragment="status-strip"]')).toContainText('1 of 2 running');
      await expect(rowOf(page, 'db').locator('[data-status="up"]')).toBeVisible();
      for (const key of ['restart', 'stop']) {
        await expect(api.locator(`[data-row-action="${key}"]`)).toHaveCount(0);
      }
      await expect(api.locator('[data-row-action="start"]')).toHaveCount(1);

      await choose(page, 'api', 'start');
      await expect(dialog(page).locator('h2')).toHaveText('Start api?');
      await expect(dialog(page)).toContainText('It exited with code');
      await confirm(page);
      await expect(toast(page)).toContainText('api started');
      await expect(api.locator('[data-status="up"]')).toBeVisible({ timeout: SETTLE_MS });
      await expect(page.locator('[data-fragment="status-strip"]')).toContainText('2 of 2 running');
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    } finally {
      await request.post(`/deployments/${id}/containers/api/start`, {
        headers: await csrfHeaders(request),
      });
    }
  });

  test('recreate after a deploy wrote a file clears Recreate to apply', async ({
    page,
    request,
    scratch,
  }) => {
    test.setTimeout(6 * SETTLE_MS);
    const id = await createDeployment(request, scratch.id, 'stack', STACK, 'block', DEMO_PROJECT);
    const linked = await request.post(`/deployments/${id}/compose-files/link`, {
      form: { compose_file: 'compose.yaml' },
      headers: await csrfHeaders(request),
    });
    expect(linked.headers()['hx-redirect']).toContain('compose_linked=1');
    const service = await createService(request, id, 'api');
    const name = `.env.e2e-${scratch.id}`;
    await createEnvironment(request, service, name, 'E2E_ACTIONS_VALUE=actions-1\n');
    try {
      await page.goto(`/services/${service}`);
      await page.getByRole('button', { name: 'Deploy service' }).click();
      const deploy = page.locator('#deploy-dialog');
      await expect(deploy.locator('[data-fragment="preflight-table"]')).toBeVisible();
      await deploy.locator('[data-deploy-submit]').click();
      await expect(deploy.locator('[data-fragment="deploy-result"]')).toHaveAttribute(
        'data-tone',
        'ok',
        { timeout: SETTLE_MS },
      );

      await page.goto(`/deployments/${id}`);
      await expect(runtime(page)).toHaveAttribute('aria-busy', 'false');
      const api = rowOf(page, 'api');
      await expect(api.locator('[data-recreate]')).toContainText('Recreate to apply');
      if (isWide(page)) {
        await expect(api.locator('[data-row-action="recreate"]')).toHaveText('Recreate');
      }
      await expect(page.locator('[data-fragment="status-strip"]')).toContainText('1 to recreate');
      const first = await startedAt(page, 'api');

      await choose(page, 'api', 'restart');
      await expect(dialog(page).locator('[data-container-warning="recreate"]')).toContainText(
        'It does not load the files deployed',
      );
      await expect(dialog(page).locator('[data-container-warning="recreate"]')).toContainText(
        'Recreate loads them.',
      );
      expect(await command(page)).toContain(`--project-directory ${STACK} -p ${DEMO_PROJECT} -f ${STACK}/compose.yaml restart api`);
      await dialog(page).getByRole('button', { name: 'Cancel' }).click();
      await expect(dialog(page)).toBeHidden();
      await expect(toast(page)).toHaveCount(0);

      await choose(page, 'api', 'recreate');
      await expect(dialog(page).locator('h2')).toHaveText('Recreate api?');
      await expect(dialog(page)).toContainText('Volumes are kept.');
      await expect(dialog(page)).toContainText('unavailable for a few seconds');
      expect(await command(page)).toBe(
        `docker compose --project-directory ${STACK} -p ${DEMO_PROJECT} -f ${STACK}/compose.yaml up -d --no-deps --force-recreate api`,
      );
      await confirm(page);
      await expect(toast(page)).toContainText('api recreated');
      await expect(api.locator('[data-recreate]')).toHaveCount(0, { timeout: SETTLE_MS });
      await expect(page.locator('[data-fragment="status-strip"]')).toContainText('All applied');
      await expect(api.locator('[data-status="up"]')).toBeVisible({ timeout: SETTLE_MS });
      await expect.poll(() => startedAt(page, 'api'), { timeout: SETTLE_MS }).not.toBe(first);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    } finally {
      rmSync(join(TARGETS, name), { force: true });
      await request.post(`/deployments/${id}/containers/api/start`, {
        headers: await csrfHeaders(request),
      });
    }
  });
});
