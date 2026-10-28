import type { Browser, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { createHostFolder } from '../host-files';
import { stateFile } from '../global-setup';
import {
  breadcrumbLinks,
  breadcrumbTypes,
  compactBreadcrumb,
  expectBreadcrumb,
  expectItemHeader,
  expectLocation,
} from '../item-header';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';
import { actionControls, rowAction } from './row-actions';
import { expect, test } from './scratch-project';

function prod(browser: Browser): Promise<number> {
  return deploymentId(browser, 'Fancy Proj. Name', 'prod');
}

function header(page: Page) {
  return page.locator('[data-fragment="page-header"]');
}

function sections(page: Page) {
  return page.locator('nav[aria-label="Sections"] a');
}

function serviceRows(page: Page) {
  return page.locator('#services [data-fragment="service-row"]');
}

function summaryRows(page: Page) {
  return page.locator('[data-fragment="service-summary-row"]');
}

function historyRows(page: Page) {
  return page.locator('#hist [data-fragment="history-row"]');
}

function recentRows(page: Page) {
  return page.locator('[data-fragment="recent-deploy-row"]');
}

test.describe('superadmin', () => {
  test('shows the page top, the tabs and the Overview with the state flags', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);

    await expectItemHeader(page, 'deployment', 'prod');
    await expectLocation(page, 'deployment', {
      root: 'Deployment path: /opt/fancy/prod',
    });
    await expect(header(page).locator('[data-status]')).toHaveCount(0);
    await expect(header(page).getByRole('link')).toHaveCount(0);
    await expect(header(page).getByRole('button')).toHaveCount(0);
    expect(await breadcrumbLinks(page)).toEqual([
      '/projects',
      expect.stringMatching(/^\/projects\/\d+$/),
    ]);
    await expectBreadcrumb(page, ['Projects', ['Fancy Proj. Name', 'Project'], ['prod', 'Deployment']]);
    await expect(breadcrumbTypes(page)).toHaveText(['Project', 'Deployment']);
    await expect(sections(page)).toHaveText([
      'Overview',
      'Files',
      'Logs',
      'History',
      'Settings',
      'Access',
    ]);
    await expect(sections(page).nth(0)).toHaveAttribute('href', `/deployments/${id}`);
    await expect(sections(page).nth(1)).toHaveAttribute('href', `/deployments/${id}/files`);
    await expect(sections(page).nth(2)).toHaveAttribute('href', `/deployments/${id}/logs`);
    await expect(sections(page).nth(3)).toHaveAttribute('href', `/deployments/${id}/activity`);
    await expect(sections(page).nth(4)).toHaveAttribute('href', `/deployments/${id}/settings`);
    await expect(sections(page).nth(5)).toHaveAttribute('href', `/deployments/${id}/access`);
    await expect(sections(page).first()).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('nav[aria-label="Sections"] [aria-current="page"]')).toHaveCount(1);

    const strip = page.locator('[data-fragment="status-strip"]');
    await expect(strip.locator('p')).toHaveText([
      'State',
      'Last deploy',
      'Drift state',
      'Dist check',
      'Post-deploy',
    ]);
    await expect(strip.locator('[data-status]').first()).toHaveText('Failed');
    await expect(strip).toContainText('root');
    await expect(strip.locator('[data-status="unchanged"]')).toHaveText([
      'Not checked yet',
      'Not checked yet',
    ]);
    await expect(strip.locator('[data-status="skipped"]')).toHaveText('Skipped');

    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    const note = page.locator('[data-runtime-state="no_project"]');
    await expect(note).toContainText('has no Docker Compose project name');
    await expect(note.getByRole('link')).toHaveAttribute('href', `/deployments/${id}/settings`);
    await expect(strip.locator('p')).toHaveCount(5);
    await expect(
      page.locator('[data-fragment="runtime-services"] th[scope="col"]'),
    ).toHaveText(['Service', 'Files', 'Last deploy', 'Actions']);
    await expect(summaryRows(page).locator('th a')).toHaveText(['mysql', 'web']);
    await expect(summaryRows(page).locator('th a').first()).toHaveAttribute(
      'href',
      /^\/services\/\d+$/,
    );
    const mysql = summaryRows(page).filter({ hasText: 'mysql' });
    await expect(mysql.locator('td').first()).toHaveText('3');
    await expect(mysql.locator('[data-status="failed"]').first()).toBeAttached();
    await expect(actionControls(mysql, 'open').first()).toHaveAttribute(
      'href',
      /^\/services\/\d+$/,
    );
    await expect(
      summaryRows(page).filter({ hasText: 'web' }).locator('[data-status="success"]'),
    ).toBeAttached();
    await expect(actionControls(summaryRows(page), 'deploy')).toHaveCount(0);
    await expect(actionControls(summaryRows(page), 'delete')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Check dist' })).toHaveCount(0);
    await expect(page.locator('#services, #hist, #deployment-files, #compose-files')).toHaveCount(0);

    await expect(recentRows(page).locator('td:nth-child(2)')).toHaveText(['mysql', 'web']);
    await expect(page.getByRole('link', { name: 'View all history' })).toHaveAttribute(
      'href',
      `/deployments/${id}/activity`,
    );
    await expect(page.locator('[data-fragment="policies-panel"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('loads the runtime after the page and keeps it when a refresh fails', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    const runtime = `**/deployments/${id}/runtime`;
    await page.route(runtime, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 600));
      await route.continue();
    });
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('#runtime')).toContainText('Reading the containers from Docker.');
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    await expect(summaryRows(page)).toHaveCount(2);
    await page.unroute(runtime);

    for (const status of [500, 404]) {
      await page.route(runtime, (route) =>
        route.fulfill({ status, contentType: 'text/html', body: '<p>refresh failed</p>' }),
      );
      const failed = page.waitForResponse(
        (response) => response.url().endsWith(`/deployments/${id}/runtime`),
      );
      await page.evaluate(() => document.body.dispatchEvent(new CustomEvent('deploy-finished')));
      expect((await failed).status()).toBe(status);
      await expect(summaryRows(page)).toHaveCount(2);
      await expect(page.locator('#runtime')).not.toContainText('refresh failed');
      await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);
      await page.unroute(runtime);
    }

    const refreshed = page.waitForResponse(
      (response) => response.url().endsWith(`/deployments/${id}/runtime`) && response.ok(),
      { timeout: 15_000 },
    );
    await refreshed;
    await expect(summaryRows(page)).toHaveCount(2);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
  });

  test('says why the containers cannot be read when Docker is not usable', async ({
    page,
    request,
    scratch,
  }) => {
    const host = createHostFolder();
    try {
      const id = await createDeployment(request, scratch.id, 'runtime-dep', host.root, 'block', 'e2e-runtime');
      await page.goto(`/deployments/${id}`);
      await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
      const state = page.locator('[data-runtime-state="unavailable"]');
      await expect(state).toContainText('The containers could not be read:');
      await expect(state.locator('[role="note"]')).toBeVisible();
      await expect(page.locator('[data-fragment="status-strip"] p')).toHaveText([
        'State',
        'Last deploy',
        'Drift state',
        'Dist check',
        'Post-deploy',
      ]);
      await expect(page.locator('[data-fragment="runtime-services"]')).toContainText('No services yet');
      await expect(page.locator('[data-fragment="runtime-network"]')).toHaveCount(0);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    } finally {
      host.remove();
    }
  });

  test('moves between the tabs and keeps the page top', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    for (const [label, path] of [
      ['Files', '/files'],
      ['Logs', '/logs'],
      ['History', '/activity'],
      ['Settings', '/settings'],
      ['Access', '/access'],
      ['Overview', ''],
    ]) {
      await sections(page).filter({ hasText: label }).click();
      await expect(page).toHaveURL(new RegExp(`/deployments/${id}${path}$`));
      await expect(sections(page).filter({ hasText: label })).toHaveAttribute(
        'aria-current',
        'page',
      );
      await expectItemHeader(page, 'deployment', 'prod');
    }
  });

  test('Files has Check dist, the services with their actions and the deployment files', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/files`);
    await expect(sections(page).filter({ hasText: 'Files' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.getByRole('link', { name: 'Check dist' })).toHaveAttribute(
      'href',
      `/deployments/${id}/dist`,
    );
    await expect(page.locator('#services th[scope="col"]')).toHaveText([
      'Service',
      'Environments',
      'Last deploy',
      'Post-deploy',
      'State flags',
      'Actions',
    ]);
    await expect(serviceRows(page).locator('th a')).toHaveText(['mysql', 'web']);
    const mysql = serviceRows(page).filter({ hasText: 'mysql' });
    await expect(mysql.locator('td').first()).toHaveText('3');
    await expect(mysql.locator('[data-status="failed"]').first()).toBeAttached();
    await expect(actionControls(mysql, 'open').first()).toHaveAttribute(
      'href',
      /^\/services\/\d+$/,
    );
    await expect(actionControls(mysql, 'deploy').first()).toBeAttached();
    await expect(actionControls(mysql, 'rename').first()).toBeAttached();
    await expect(actionControls(mysql, 'delete').first()).toBeAttached();
    await expect(serviceRows(page).filter({ hasText: 'web' })).toContainText('No flags');
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveAttribute(
      'href',
      `/deployments/${id}/services/new`,
    );
    await expect(page.locator('#deployment-files')).toBeVisible();
    await expect(page.locator('#deploy-dialog')).toBeAttached();
    await expect(page.locator('#service-delete')).toBeAttached();
    await expect(page.locator('[data-fragment="status-strip"]')).toHaveCount(0);
    await expect(page.locator('[data-fragment="policies-panel"]')).toHaveCount(0);
    await expect(page.locator('#compose-files')).toHaveCount(0);
    await expect(page.locator('#hist')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('History has the deploy history and expands the details of a run', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/activity`);
    await expect(sections(page).filter({ hasText: 'History' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.locator('#hist th[scope="col"]')).toHaveText([
      'Time',
      'Service',
      'Who',
      'Environments',
      'Result',
      'Post-deploy',
      'Details',
    ]);
    await expect(historyRows(page).locator('td:nth-child(2)')).toHaveText(['mysql', 'web']);
    await expect(historyRows(page).first()).toContainText('.env.db v2, .env.db.secrets v1');
    await expect(page.locator('#services')).toHaveCount(0);
    const toggles = page.locator('#hist [data-js="row-toggle"]');
    const newest = page.locator(`#${await toggles.first().getAttribute('aria-controls')}`);
    const older = page.locator(`#${await toggles.nth(1).getAttribute('aria-controls')}`);
    await expect(toggles.first()).toHaveAttribute('aria-expanded', 'true');
    await expect(newest).toBeVisible();
    await expect(newest).toContainText('see result.');
    await expect(older).toBeHidden();
    await toggles.nth(1).click();
    await expect(toggles.nth(1)).toHaveAttribute('aria-expanded', 'true');
    await expect(older).toBeVisible();
    await expect(older).toContainText('.env.web v1 written.');
    await expect(older).toContainText('Post-deploy exit code 0, output truncated.');
    await toggles.first().click();
    await expect(newest).toBeHidden();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('Settings has the policies, the Compose files, Edit and the delete zone', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/settings`);
    await expect(sections(page).filter({ hasText: 'Settings' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    const policies = page.locator('[data-fragment="policies-panel"]');
    await expect(policies.locator('dt')).toHaveText([
      'Dist keys missing',
      'Drift',
      'Target missing',
      'Default file mode, owner',
    ]);
    await expect(policies.locator('dd')).toHaveText([
      'Block',
      'Halt',
      'Create',
      '0600, www-data:www-data',
    ]);
    await expect(policies.getByRole('link', { name: 'Edit policies' })).toHaveAttribute(
      'href',
      `/deployments/${id}/edit`,
    );
    await expect(page.getByRole('link', { name: 'Edit deployment' })).toHaveAttribute(
      'href',
      `/deployments/${id}/edit`,
    );
    await expect(page.locator('#compose-files')).toBeVisible();
    await expect(page.locator('#compose-link-open')).toBeVisible();
    await expect(page.locator('#deployment-delete-open')).toBeVisible();
    await expect(page.locator('#deployment-delete')).toBeAttached();
    await expect(page.locator('#services, #hist, [data-fragment="status-strip"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('is reached from the project page and goes back through the breadcrumb', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto('/projects');
    await page.locator('[data-fragment="project-row"] th a', { hasText: /^Fancy Proj\. Name$/ }).click();
    const row = page.locator('[data-fragment="deployment-row"]', { hasText: '/opt/fancy/prod' });
    await (await rowAction(row, 'open')).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${id}$`));
    const breadcrumb = page.locator('nav[data-breadcrumb="full"]');
    if (isWide(page)) {
      await expect(compactBreadcrumb(page)).toBeHidden();
      await breadcrumb.getByRole('link', { name: 'Fancy Proj. Name' }).click();
    } else {
      await expect(breadcrumb).toBeHidden();
      await expect(compactBreadcrumb(page).locator('li:not([aria-hidden="true"])')).toHaveText([
        'Fancy Proj. Name Project',
        'prod Deployment',
      ]);
      await compactBreadcrumb(page).getByRole('link', { name: /Fancy Proj\. Name/ }).click();
    }
    await expect(page).toHaveURL(/\/projects\/\d+$/);
  });

  test('refreshes the services panel of Files when a deploy finishes', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/files`);
    const services = page.waitForResponse(
      (response) => response.url().endsWith(`/deployments/${id}/services`) && response.ok(),
    );
    await page.evaluate(() => document.body.dispatchEvent(new CustomEvent('deploy-finished')));
    await services;
    await expect(serviceRows(page)).toHaveCount(2);
  });

  test('refreshes the history panel of History when a deploy finishes', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/activity`);
    const history = page.waitForResponse(
      (response) => response.url().endsWith(`/deployments/${id}/history`) && response.ok(),
    );
    await page.evaluate(() => document.body.dispatchEvent(new CustomEvent('deploy-finished')));
    await history;
    await expect(historyRows(page)).toHaveCount(2);
  });

  test('marks a deployment outside the allowed paths and disables the check on Files', async ({
    page,
    browser,
  }) => {
    const id = await deploymentId(browser, 'billing', 'legacy');
    await page.goto(`/deployments/${id}`);
    await expect(header(page).locator('[data-location="root"]')).toHaveText(
      'Deployment path: /srv/old/legacy',
    );
    const note =
      'This deployment is outside the allowed paths. It cannot be deployed or checked until the path is allowed again or the deployment is moved.';
    await expect(page.locator('main > [role="note"]')).toHaveText(note);
    await page.goto(`/deployments/${id}/files`);
    await expect(page.locator('main > [role="note"]')).toHaveText(note);
    await expect(page.getByRole('button', { name: 'Check dist' })).toBeDisabled();
    await expect(page.getByRole('link', { name: 'Check dist' })).toHaveCount(0);
    await expect(serviceRows(page).locator('th a')).toHaveText(['cron']);
    await expect(page.getByRole('button', { name: 'Deploy', exact: true })).toHaveCount(0);
    await expect(page.locator('#deploy-dialog')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await page.goto(`/deployments/${id}/settings`);
    await expect(page.getByRole('link', { name: 'Edit deployment' })).toBeVisible();
  });

  test('answers 404 for a deployment that does not exist', async ({ page }) => {
    for (const path of ['', '/files', '/activity', '/settings']) {
      const response = await page.goto(`/deployments/999999${path}`);
      expect(response?.status()).toBe(404);
      await expect(page.locator('main')).toContainText('Not found');
    }
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('checks and deploys but has no structure actions', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/files`);
    await expect(page.getByRole('link', { name: 'Check dist' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Edit', exact: true })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveCount(0);
    await expect(sections(page)).toHaveText(['Overview', 'Files', 'Logs', 'History', 'Settings']);
    await expect(serviceRows(page).locator('[data-row-action="deploy"]')).toHaveCount(2);
    await expect(actionControls(serviceRows(page), 'delete')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.goto(`/deployments/${id}/activity`);
    await expect(page.locator('#hist')).toContainText('Post-deploy exit code');

    await page.goto(`/deployments/${id}/settings`);
    await expect(page.getByRole('link', { name: 'Edit policies' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Edit deployment' })).toHaveCount(0);
    await expect(page.locator('#deployment-delete-open')).toHaveCount(0);
    await expect(page.locator('[data-fragment="policies-panel"]')).toContainText('Read-only');
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('reads the Overview with no structure, check, deploy or output control', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expectItemHeader(page, 'deployment', 'prod');
    await expect(header(page).getByRole('link')).toHaveCount(0);
    await expect(header(page).getByRole('button')).toHaveCount(0);
    await expect(summaryRows(page).locator('th a')).toHaveText(['mysql', 'web']);
    await expect(actionControls(summaryRows(page), 'deploy')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Check dist' })).toHaveCount(0);
    await expect(sections(page)).toHaveText(['Overview', 'Files', 'History', 'Settings']);
  });

  test('reads Files, History and Settings without edit, deploy or output', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}/files`);
    await expect(serviceRows(page).locator('th a')).toHaveText(['mysql', 'web']);
    await expect(actionControls(serviceRows(page), 'deploy')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Check dist' })).toHaveCount(0);
    await page.goto(`/deployments/${id}/activity`);
    await expect(page.locator('#hist')).not.toContainText('exit code');
    await page.goto(`/deployments/${id}/settings`);
    await expect(page.locator('[data-fragment="policies-panel"]')).toContainText('Read-only');
    await expect(page.locator('#deployment-delete-open')).toHaveCount(0);
  });

  test('cannot open a deployment of a project outside the grants', async ({ page, browser }) => {
    const id = await deploymentId(browser, 'billing', 'main');
    for (const path of ['', '/files', '/activity', '/settings']) {
      const response = await page.goto(`/deployments/${id}${path}`);
      expect(response?.status()).toBe(404);
      await expect(page.locator('main')).toContainText('Not found');
      await expect(page.locator('main')).not.toContainText('/opt/billing');
    }
  });
});

test.describe('scoped users', () => {
  test.describe('deployment grant', () => {
    test.use({ storageState: stateFile('deploymentAdmin') });

    test('sees the whole deployment it was granted', async ({ page, browser }) => {
      const id = await prod(browser);
      await page.goto(`/deployments/${id}`);
      await expect(summaryRows(page).locator('th a')).toHaveText(['mysql', 'web']);
      await expectLocation(page, 'deployment', {
        root: 'Deployment path: /opt/fancy/prod',
      });
      await expect(page.locator('main > [role="note"]')).toHaveCount(0);
      await expect(sections(page)).toHaveText(['Overview', 'Files', 'Logs', 'History', 'Settings']);
      await page.goto(`/deployments/${id}/settings`);
      await expect(page.locator('[data-fragment="policies-panel"]')).toBeVisible();
    });
  });

  test.describe('environment grant', () => {
    test.use({ storageState: stateFile('envScopedAdmin') });

    test('sees the parent by name with the one service and file it leads to', async ({
      page,
      browser,
    }) => {
      const id = await prod(browser);
      await page.goto(`/deployments/${id}`);
      await expectLocation(page, 'deployment', {});
      await expectBreadcrumb(page, ['Projects', ['Fancy Proj. Name', 'Project'], ['prod', 'Deployment']]);
      await expect(page.locator('main > [role="note"]')).toHaveText(
        'You have access to one environment of this deployment. Only its service is shown.',
      );
      await expect(sections(page)).toHaveText(['Overview', 'Files', 'History']);
      await expect(
        page.locator('[data-fragment="runtime-services"] th[scope="col"]').nth(1),
      ).toHaveText('Files in your scope');
      await expect(summaryRows(page).locator('th a')).toHaveText(['mysql']);
      await expect(summaryRows(page).locator('td').first()).toHaveText('1');
      await expect(recentRows(page)).toHaveCount(1);

      await page.goto(`/deployments/${id}/files`);
      await expect(page.locator('#services th[scope="col"]').nth(1)).toHaveText(
        'Environments in your scope',
      );
      await expect(serviceRows(page).locator('th a')).toHaveText(['mysql']);
      await expect(serviceRows(page).locator('td').first()).toHaveText('1');

      await page.goto(`/deployments/${id}/activity`);
      await expect(historyRows(page)).toHaveCount(1);
      await expect(historyRows(page).first()).toContainText('.env.db v2');
      await expect(page.locator('#hist')).toContainText('Post-deploy exit code n/a');
      const main = page.locator('main');
      for (const hidden of ['.env.db.secrets', '.env.web', '/opt/fancy', 'Policies']) {
        await expect(main).not.toContainText(hidden);
      }
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    });

    test('is refused the Settings tab', async ({ page, browser }) => {
      const response = await page.goto(`/deployments/${await prod(browser)}/settings`);
      expect(response?.status()).toBe(403);
    });
  });

  test.describe('environment viewer', () => {
    test.use({ storageState: stateFile('envScopedViewer') });

    test('has no check or deploy on its file', async ({ page, browser }) => {
      await page.goto(`/deployments/${await prod(browser)}/files`);
      await expect(serviceRows(page).locator('th a')).toHaveText(['mysql']);
      await expect(page.getByRole('link', { name: 'Check dist' })).toHaveCount(0);
      await expect(actionControls(serviceRows(page), 'deploy')).toHaveCount(0);
    });
  });
});

test.describe('layout on small screens', () => {
  test('keeps each tab within the viewport and scrolls the tables inside', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    for (const [path, panel] of [
      ['', '[data-fragment="runtime-services"]'],
      ['/files', '#services'],
      ['/activity', '#hist'],
    ]) {
      await page.goto(`/deployments/${id}${path}`);
      if (path === '') {
        await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
      }
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
      if (!isWide(page)) {
        if (path === '') {
          await expect(page.locator('[data-fragment="service-summary-item"]')).toHaveCount(2);
          await expect(summaryRows(page).first()).toBeHidden();
          continue;
        }
        const scroller = page.locator(`${panel} .overflow-x-auto`);
        expect(await scroller.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
          true,
        );
      }
    }
    await page.goto(`/deployments/${id}/settings`);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test.describe('on a phone', () => {
    test.use({ viewport: { width: 360, height: 800 } });

    test('scrolls the tabs sideways inside their bar with 44 px targets @narrow', async ({
      page,
      browser,
    }) => {
      await page.goto(`/deployments/${await prod(browser)}`);
      const bar = page.locator('nav[aria-label="Sections"]');
      expect(await bar.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
      for (const tab of await sections(page).all()) {
        const box = await tab.boundingBox();
        expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
        expect(box?.width ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
      }
      await sections(page).filter({ hasText: 'Access' }).scrollIntoViewIfNeeded();
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    });
  });
});
