import type { Browser, Locator, Page } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { stateFile } from '../global-setup';
import { expectItemHeader } from '../item-header';
import { serviceId } from '../services-api';
import { horizontalOverflow, isWide } from '../viewport';
import { actionControls, rowAction } from './row-actions';

const LG_BREAKPOINT = 1024;

function width(page: Page): number {
  return page.viewportSize()?.width ?? 0;
}

function stagingWeb(browser: Browser): Promise<number> {
  return serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
}

function prodMysql(browser: Browser): Promise<number> {
  return serviceId(browser, 'Fancy Proj. Name', 'prod', 'mysql');
}

function header(page: Page) {
  return page.locator('[data-fragment="page-header"]');
}

function environmentsPanel(page: Page) {
  return page.locator('#environments');
}

/** Environment rows of whichever layout is visible: the table from md up, the list below. */
function visibleRows(page: Page) {
  const selector = isWide(page) ? 'tr[data-env]' : 'li[data-fragment="env-row"]';
  return environmentsPanel(page).locator(selector);
}

function statuses(page: Page) {
  return environmentsPanel(page).locator(
    isWide(page) ? 'tr[data-env] [data-status]' : 'li [data-status]',
  );
}

async function expectLayout(page: Page): Promise<void> {
  const wide = isWide(page);
  const table = environmentsPanel(page).locator('table');
  if (wide) {
    await expect(table).toBeVisible();
  } else {
    await expect(table).toBeHidden();
  }
  const explainer = page.locator('section[data-fragment="service-explainer"]');
  const details = page.locator('details[data-fragment="service-explainer"]');
  if (width(page) >= LG_BREAKPOINT) {
    await expect(explainer).toBeVisible();
    await expect(details).toBeHidden();
  } else {
    await expect(explainer).toBeHidden();
    await expect(details).toBeVisible();
  }
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
}

test.describe('superadmin', () => {
  test('shows the header, every status, the command settings and the history', async ({
    page,
    browser,
  }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);

    await expectItemHeader(page, 'service', 'web');
    await expect(header(page).locator('[data-status]')).toHaveText(['1 pending']);
    await expect(header(page).locator('p')).toContainText('Deployment staging in Fancy Proj. Name');
    await expect(header(page).locator('p')).toContainText('/opt/rentals/staging');
    await expect(page.locator('nav[aria-label="Sections"] a')).toHaveText([
      'Environments 4',
      'Deploy history',
      'AccessSuperadmin',
    ]);
    await expect(page.locator('nav[aria-label="Breadcrumb"] li')).toHaveText([
      'Projects',
      '/',
      'Fancy Proj. Name',
      '/',
      'staging',
      '/',
      'web',
    ]);
    await expectLayout(page);

    await expect(visibleRows(page)).toHaveCount(4);
    await expect(statuses(page)).toHaveText(['Deployed', 'Blocked', 'Pending', 'Never deployed']);
    await expect(environmentsPanel(page).getByRole('link', { name: 'Add environment' })).toHaveAttribute(
      'href',
      `/services/${id}/environments/new`,
    );
    await expect(actionControls(environmentsPanel(page), 'dist').first()).toBeAttached();

    const postDeploy = page.locator('#postdeploy');
    await expect(postDeploy).toContainText('by admin · exit code 0');
    await expect(postDeploy.locator('pre').first()).toHaveText('web recreated');
    await expect(postDeploy.locator('[data-fragment="postdeploy-settings"]')).toContainText(
      'docker compose up -d --no-deps web',
    );
    await expect(postDeploy.getByRole('button')).toHaveText(['Test command', 'Save settings']);

    await expect(page.locator('#hist [data-fragment="history-row"]')).toHaveCount(2);
  });

  test(
    'fits the environments table at 1280px without a sideways scroll',
    { tag: '@wide' },
    async ({ page, browser }) => {
      const services = [
        await stagingWeb(browser),
        await prodMysql(browser),
        await serviceId(browser, 'demo', 'stack', 'api'),
        await serviceId(browser, 'demo', 'stack', 'db'),
      ];
      for (const id of services) {
        await page.goto(`/services/${id}`);
        const wrapper = environmentsPanel(page).locator('.overflow-x-auto');
        const [scrolls, shows] = await wrapper.evaluate((element) => [
          element.scrollWidth,
          element.clientWidth,
        ]);
        expect(scrolls, `service ${id}`).toBeLessThanOrEqual(shows);
        expect(await horizontalOverflow(page), `service ${id}`).toBeLessThanOrEqual(0);
      }
    },
  );

  test('puts the explanation of a service at the very bottom, below the deploy history', async ({
    page,
    browser,
  }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const explainer = page.locator(
      width(page) >= LG_BREAKPOINT
        ? 'section[data-fragment="service-explainer"]'
        : 'details[data-fragment="service-explainer"]',
    );
    await expect(explainer).toBeVisible();
    const box = async (locator: Locator) => (await locator.boundingBox()) ?? { y: 0, height: 0 };
    const explainerTop = (await box(explainer)).y;
    for (const panel of ['#environments', '#postdeploy', '#hist']) {
      const above = await box(page.locator(panel));
      expect(above.y + above.height, panel).toBeLessThanOrEqual(explainerTop);
    }
    const pageHeight = await page.evaluate(() => document.documentElement.scrollHeight);
    expect(explainerTop + (await box(explainer)).height).toBeGreaterThan(pageHeight - 120);
  });

  test('is reached from the deployment page and lists a failed run', async ({ page, browser }) => {
    const deployment = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    await page.goto(`/deployments/${deployment}`);
    const firstService = page.locator('#services [data-fragment="service-row"]').first();
    await (await rowAction(firstService, 'open')).click();
    await expect(page).toHaveURL(/\/services\/\d+$/);
    await expectItemHeader(page, 'service', 'mysql');
    await expect(statuses(page)).toHaveText(['Failed', 'Never deployed', 'Failed']);
    await expect(page.locator('#postdeploy')).toContainText('Skipped');
    await expect(page.locator('#postdeploy')).toContainText('deploy did not finish');
    await expectLayout(page);
  });

  test('shows the empty states of a service without files or runs', async ({ page, browser }) => {
    const id = await serviceId(browser, 'billing', 'legacy', 'cron');
    await page.goto(`/services/${id}`);
    await expect(environmentsPanel(page).locator('[data-fragment="empty-state"]')).toContainText(
      'No environments yet',
    );
    await expect(
      environmentsPanel(page).getByRole('link', { name: 'Add environment' }),
    ).toHaveCount(2);
    await expect(page.locator('#hist [data-fragment="empty-state"]')).toContainText('No deploys yet');
    await expect(page.locator('#postdeploy')).toContainText('No deploys yet.');
    await expect(page.locator('#postdeploy #cmds')).toHaveValue('');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the panels refresh from their own fragment URLs', async ({ page, browser }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);
    for (const [name, fragment] of [
      ['environments', 'environments-table'],
      ['postdeploy', 'postdeploy-panel'],
      ['history', 'deploy-history'],
    ]) {
      const response = await page.request.get(`/services/${id}/${name}`, {
        headers: { 'HX-Request': 'true' },
      });
      expect(response.status()).toBe(200);
      expect(response.headers()['cache-control']).toBe('no-store');
      expect(await response.text()).toContain(`data-fragment="${fragment}"`);
    }
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('edits, checks dist and reads output but sees no command or structure actions', async ({
    page,
    browser,
  }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);
    await expectLayout(page);
    await expect(visibleRows(page)).toHaveCount(4);
    await expect(actionControls(environmentsPanel(page), 'edit').first()).toBeAttached();
    await expect(actionControls(environmentsPanel(page), 'dist').first()).toBeAttached();
    await expect(environmentsPanel(page).getByRole('link', { name: 'Add environment' })).toHaveCount(0);
    const postDeploy = page.locator('#postdeploy');
    await expect(postDeploy.locator('pre')).toHaveText('web recreated');
    await expect(postDeploy).toContainText('Command text and settings are visible to the superadmin only.');
    await expect(page.locator('[data-fragment="postdeploy-settings"]')).toHaveCount(0);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('is read only: view and history, status only post-deploy', async ({ page, browser }) => {
    const id = await stagingWeb(browser);
    await page.goto(`/services/${id}`);
    await expectLayout(page);
    await expect(page.locator('[role="note"]').first()).toContainText(
      'Read-only view. Editing and deploying are not available for the Viewer group.',
    );
    const panel = environmentsPanel(page);
    await expect(actionControls(panel, 'view').first()).toBeAttached();
    await expect(actionControls(panel, 'history').first()).toBeAttached();
    for (const absent of ['edit', 'dist']) {
      await expect(actionControls(panel, absent)).toHaveCount(0);
    }
    await expect(panel.getByRole('link', { name: 'Add environment' })).toHaveCount(0);
    const postDeploy = page.locator('#postdeploy');
    await expect(postDeploy).toContainText('Output and command text are not available for the Viewer group.');
    await expect(postDeploy.locator('pre')).toHaveCount(0);
    await expect(postDeploy).not.toContainText('by admin');
  });
});

test.describe('environment grant', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('sees one file, notes the limit and never names the others', async ({ page, browser }) => {
    const id = await prodMysql(browser);
    await page.goto(`/services/${id}`);
    await expectLayout(page);
    await expect(visibleRows(page)).toHaveCount(1);
    await expect(page.locator('nav[aria-label="Sections"] a').first()).toHaveText('Environments 1');
    await expect(page.locator('main')).toContainText(
      'You see 1 environment of this service. Deploys write only the environments you can see.',
    );
    await expect(page.locator('#postdeploy')).toContainText(
      'This service also loads environments outside your access.',
    );
    await expect(header(page).locator('p')).not.toContainText('/opt/rentals');
    await expect(page.locator('main')).not.toContainText('.env.db.secrets');
  });
});

test.describe('deployment grant', () => {
  test.use({ storageState: stateFile('deploymentAdmin') });

  test('sees the root path and every file of the service', async ({ page, browser }) => {
    const id = await prodMysql(browser);
    await page.goto(`/services/${id}`);
    await expect(header(page).locator('p')).toContainText('/opt/rentals/prod');
    await expect(visibleRows(page)).toHaveCount(3);
    await expect(page.locator('[role="note"]')).toHaveCount(0);
  });
});

test.describe('no access', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('a service outside the scope answers 404 on the page and every panel', async ({
    page,
    browser,
  }) => {
    const id = await prodMysql(browser);
    for (const suffix of ['', '/environments', '/postdeploy', '/history']) {
      const response = await page.request.get(`/services/${id}${suffix}`, {
        headers: suffix ? { 'HX-Request': 'true' } : {},
      });
      expect(response.status()).toBe(404);
    }
  });
});
