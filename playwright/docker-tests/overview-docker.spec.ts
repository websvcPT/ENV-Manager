import { rmSync } from 'node:fs';
import { join } from 'node:path';

import type { Locator, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { createService } from '../services-api';
import { expect, test } from '../tests/scratch-project';
import { horizontalOverflow, isWide } from '../viewport';

/*
 * The Overview of a deployment with a usable Docker, on the demo stack: the running containers
 * of the demo Compose project with their state, health and addresses, the project network, and
 * the Recreate to apply warning after a deploy that no command followed. `make e2e-demo` starts
 * the demo workloads before it runs these specs.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const TARGETS = join(STACK, 'environments');
const DEMO_PROJECT = 'env-manager-demo';
const NETWORK = `${DEMO_PROJECT}_default`;
const DEPLOY_WAIT_MS = 30_000;

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

function cell(page: Page, label: string): Locator {
  return page
    .locator('[data-fragment="status-strip"] > div')
    .filter({ has: page.getByText(label, { exact: true }) });
}

test.beforeAll(() => {
  expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
});

test.describe('Overview with a usable Docker', () => {
  test('lists the running demo containers, the healthy one and the project network', async ({
    page,
    browser,
  }) => {
    const id = await deploymentId(browser, 'demo', 'stack');
    await page.goto(`/deployments/${id}`);
    await expect(runtime(page)).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('[data-runtime-state]')).toHaveCount(0);

    const api = rowOf(page, 'api');
    const db = rowOf(page, 'db');
    await expect(api.locator('[data-status="up"]')).toHaveText('Running');
    await expect(api.locator('[data-status="healthy"]')).toHaveText('Healthy');
    await expect(db.locator('[data-status="up"]')).toHaveText('Running');
    await expect(db.locator('[data-status="healthy"], [data-status="unhealthy"]')).toHaveCount(0);
    await expect(api.locator('[data-image]')).toHaveText('alpine:3.24.2');
    await expect(db.locator('[data-image]')).toHaveText('alpine:3.24.2');
    await expect(api.locator('[data-uptime]')).toContainText(/Up \d+ (second|minute|hour|day)s?/);
    await expect(api.locator('[data-files-count]')).toHaveText('2');
    await expect(db.locator('[data-files-count]')).toHaveText('2');
    await expect(api.locator('a[href^="/services/"]').first()).toBeVisible();

    await expect(cell(page, 'Containers')).toContainText('2 of 2 running');
    await expect(cell(page, 'Applied')).toBeVisible();

    const network = page.locator(`[data-fragment="network-row"][data-network="${NETWORK}"]`);
    await expect(network).toContainText('bridge');
    await expect(network).toContainText(/\d+\.\d+\.\d+\.\d+\/\d+/);
    await expect(network).toContainText('None');
    for (const service of ['api', 'db']) {
      const addresses = page.locator(
        `[data-fragment="network-service-row"][data-service="${service}"]`,
      );
      await expect(addresses).toContainText(NETWORK);
      await expect(addresses).toContainText(/\d+\.\d+\.\d+\.\d+/);
      await expect(addresses.locator('[data-port]')).toHaveCount(0);
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    if (!isWide(page)) {
      await expect(page.locator('[data-fragment="service-summary-item"]:visible')).toHaveCount(2);
      for (const scroller of await page
        .locator('[data-fragment="runtime-network"] .overflow-x-auto')
        .all()) {
        expect(await scroller.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
      }
    }
  });

  test('marks a service Recreate to apply after a deploy that wrote a file and no command ran', async ({
    page,
    request,
    scratch,
  }) => {
    test.setTimeout(DEPLOY_WAIT_MS + 30_000);
    const id = await createDeployment(request, scratch.id, 'stack', STACK, 'block', DEMO_PROJECT);
    const service = await createService(request, id, 'api');
    const name = `.env.e2e-${scratch.id}`;
    await createEnvironment(request, service, name, 'E2E_OVERVIEW_VALUE=overview-1\n');
    try {
      await page.goto(`/deployments/${id}`);
      await expect(runtime(page)).toHaveAttribute('aria-busy', 'false');
      await expect(rowOf(page, 'api').locator('[data-status="up"]')).toBeVisible();
      await expect(rowOf(page, 'api').locator('[data-recreate]')).toHaveCount(0);
      await expect(cell(page, 'Applied')).toContainText('All applied');
      const compose = rowOf(page, 'db');
      await expect(compose).toContainText('No files in ENV Manager');
      await expect(compose.locator('[data-add-service="db"]')).toHaveAttribute(
        'href',
        `/deployments/${id}/services/new?name=db`,
      );

      await page.goto(`/services/${service}`);
      await page.getByRole('button', { name: 'Deploy service' }).click();
      const dialog = page.locator('#deploy-dialog');
      await expect(dialog.locator('[data-fragment="preflight-table"]')).toBeVisible();
      await dialog.locator('[data-deploy-submit]').click();
      await expect(dialog.locator('[data-fragment="deploy-result"]')).toHaveAttribute(
        'data-tone',
        'ok',
        { timeout: DEPLOY_WAIT_MS },
      );

      await page.goto(`/deployments/${id}`);
      await expect(runtime(page)).toHaveAttribute('aria-busy', 'false');
      const marked = rowOf(page, 'api').locator('[data-recreate]');
      await expect(marked).toContainText('Recreate to apply');
      await expect(marked).toContainText(/Files written \d{4}-\d\d-\d\d \d\d:\d\d/);
      await expect(marked).toContainText('container created');
      await expect(cell(page, 'Applied')).toContainText('1 to recreate');
      await expect(rowOf(page, 'db').locator('[data-recreate]')).toHaveCount(0);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    } finally {
      rmSync(join(TARGETS, name), { force: true });
    }
  });
});
