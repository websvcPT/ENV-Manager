import type { Browser, Page } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { stateFile } from '../global-setup';
import { expectItemHeader, expectLocation, locationLinks } from '../item-header';
import { horizontalOverflow, isWide } from '../viewport';
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

function historyRows(page: Page) {
  return page.locator('#hist [data-fragment="history-row"]');
}

test.describe('superadmin', () => {
  test('shows the header, status strip, services, history and policies', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);

    await expectItemHeader(page, 'deployment', 'prod');
    await expect(header(page).locator('[data-status]')).toHaveText(['Failed']);
    await expectLocation(page, 'deployment', {
      parents: 'Project: Fancy Proj. Name',
      root: 'Deployment path: /opt/fancy/prod',
    });
    expect(await locationLinks(page, 'deployment')).toEqual([expect.stringMatching(/^\/projects\/\d+$/)]);
    await expect(header(page).getByRole('link', { name: 'Check dist' })).toHaveAttribute(
      'href',
      `/deployments/${id}/dist`,
    );
    await expect(header(page).getByRole('link', { name: 'Edit' })).toHaveAttribute(
      'href',
      `/deployments/${id}/edit`,
    );
    await expect(page.locator('nav[aria-label="Breadcrumb"] li')).toHaveText([
      'Projects',
      '/',
      'Fancy Proj. Name',
      '/',
      'prod',
    ]);
    await expect(sections(page)).toHaveText(['Services', 'Deploy history', 'AccessSuperadmin']);
    await expect(sections(page).first()).toHaveAttribute('aria-current', 'page');

    const strip = page.locator('[data-fragment="status-strip"]');
    await expect(strip.locator('p')).toHaveText([
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
    await expect(mysql.locator('[data-status="failed"]').first()).toBeVisible();
    await expect(actionControls(mysql, 'open').first()).toHaveAttribute(
      'href',
      /^\/services\/\d+$/,
    );
    await expect(actionControls(mysql, 'deploy').first()).toBeAttached();
    await expect(serviceRows(page).filter({ hasText: 'web' })).toContainText('No flags');
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveAttribute(
      'href',
      `/deployments/${id}/services/new`,
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
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('expands and collapses the details of a run', async ({ page, browser }) => {
    await page.goto(`/deployments/${await prod(browser)}`);
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
    const breadcrumb = page.locator('nav[aria-label="Breadcrumb"]');
    if (isWide(page)) {
      await breadcrumb.getByRole('link', { name: 'Fancy Proj. Name' }).click();
    } else {
      await expect(breadcrumb).toBeHidden();
      await page.goBack();
    }
    await expect(page).toHaveURL(/\/projects\/\d+$/);
  });

  test('refreshes the services and history panels when a deploy finishes', async ({
    page,
    browser,
  }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    const services = page.waitForResponse(
      (response) => response.url().endsWith(`/deployments/${id}/services`) && response.ok(),
    );
    const history = page.waitForResponse(
      (response) => response.url().endsWith(`/deployments/${id}/history`) && response.ok(),
    );
    await page.evaluate(() => document.body.dispatchEvent(new CustomEvent('deploy-finished')));
    await Promise.all([services, history]);
    await expect(serviceRows(page)).toHaveCount(2);
    await expect(historyRows(page)).toHaveCount(2);
  });

  test('marks a deployment outside the allowed paths and disables the check', async ({
    page,
    browser,
  }) => {
    const id = await deploymentId(browser, 'billing', 'legacy');
    await page.goto(`/deployments/${id}`);
    await expect(header(page).locator('[data-location="root"]')).toHaveText(
      'Deployment path: /srv/old/legacy',
    );
    await expect(page.locator('[role="note"]')).toHaveText(
      'This deployment is outside the allowed paths. It cannot be deployed or checked until the path is allowed again or the deployment is moved.',
    );
    await expect(header(page).getByRole('button', { name: 'Check dist' })).toBeDisabled();
    await expect(header(page).getByRole('link', { name: 'Check dist' })).toHaveCount(0);
    await expect(header(page).getByRole('link', { name: 'Edit' })).toBeVisible();
    await expect(serviceRows(page).locator('th a')).toHaveText(['cron']);
    await expect(page.getByRole('button', { name: 'Deploy', exact: true })).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('answers 404 for a deployment that does not exist', async ({ page }) => {
    const response = await page.goto('/deployments/999999');
    expect(response?.status()).toBe(404);
    await expect(page.locator('main')).toContainText('Not found');
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('checks and deploys but has no structure actions', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expect(header(page).getByRole('link', { name: 'Check dist' })).toBeVisible();
    await expect(header(page).getByRole('link', { name: 'Edit' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Edit policies' })).toHaveCount(0);
    await expect(page.locator('[data-fragment="policies-panel"]')).toContainText('Read-only');
    await expect(sections(page)).toHaveText(['Services', 'Deploy history']);
    await expect(serviceRows(page).locator('[data-row-action="deploy"]')).toHaveCount(2);
    await expect(page.locator('#hist')).toContainText('Post-deploy exit code');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('reads the deployment with no check, deploy, edit or output', async ({ page, browser }) => {
    const id = await prod(browser);
    await page.goto(`/deployments/${id}`);
    await expectItemHeader(page, 'deployment', 'prod');
    await expect(header(page).getByRole('link')).toHaveCount(1);
    await expect(header(page).getByRole('button')).toHaveCount(0);
    await expect(serviceRows(page).locator('th a')).toHaveText(['mysql', 'web']);
    await expect(actionControls(serviceRows(page), 'deploy')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Add service' })).toHaveCount(0);
    await expect(page.locator('#hist')).not.toContainText('exit code');
    await expect(page.locator('[data-fragment="policies-panel"]')).toContainText('Read-only');
  });

  test('cannot open a deployment of a project outside the grants', async ({ page, browser }) => {
    const id = await deploymentId(browser, 'billing', 'main');
    const response = await page.goto(`/deployments/${id}`);
    expect(response?.status()).toBe(404);
    await expect(page.locator('main')).toContainText('Not found');
    await expect(page.locator('main')).not.toContainText('/opt/billing');
  });
});

test.describe('scoped users', () => {
  test.describe('deployment grant', () => {
    test.use({ storageState: stateFile('deploymentAdmin') });

    test('sees the whole deployment it was granted', async ({ page, browser }) => {
      await page.goto(`/deployments/${await prod(browser)}`);
      await expect(serviceRows(page).locator('th a')).toHaveText(['mysql', 'web']);
      await expectLocation(page, 'deployment', {
        parents: 'Project: Fancy Proj. Name',
        root: 'Deployment path: /opt/fancy/prod',
      });
      await expect(page.locator('[data-fragment="policies-panel"]')).toBeVisible();
      await expect(page.locator('[role="note"]')).toHaveCount(0);
    });
  });

  test.describe('environment grant', () => {
    test.use({ storageState: stateFile('envScopedAdmin') });

    test('sees the parent by name with the one service and file it leads to', async ({
      page,
      browser,
    }) => {
      await page.goto(`/deployments/${await prod(browser)}`);
      await expectLocation(page, 'deployment', { parents: 'Project: Fancy Proj. Name' });
      await expect(page.locator('[role="note"]')).toHaveText(
        'You have access to one environment of this deployment. Only its service is shown.',
      );
      await expect(page.locator('#services th[scope="col"]').nth(1)).toHaveText(
        'Environments in your scope',
      );
      await expect(serviceRows(page).locator('th a')).toHaveText(['mysql']);
      await expect(serviceRows(page).locator('td').first()).toHaveText('1');
      await expect(historyRows(page)).toHaveCount(1);
      await expect(historyRows(page).first()).toContainText('.env.db v2');
      await expect(page.locator('#hist')).toContainText('Post-deploy exit code n/a');
      const main = page.locator('main');
      for (const hidden of ['.env.db.secrets', '.env.web', '/opt/fancy', 'Policies']) {
        await expect(main).not.toContainText(hidden);
      }
      await expect(page.locator('[data-fragment="policies-panel"]')).toHaveCount(0);
      await expect(header(page).getByRole('link', { name: 'Edit' })).toHaveCount(0);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    });
  });

  test.describe('environment viewer', () => {
    test.use({ storageState: stateFile('envScopedViewer') });

    test('has no check or deploy on its file', async ({ page, browser }) => {
      await page.goto(`/deployments/${await prod(browser)}`);
      await expect(serviceRows(page).locator('th a')).toHaveText(['mysql']);
      await expect(header(page).getByRole('link')).toHaveCount(1);
      await expect(actionControls(serviceRows(page), 'deploy')).toHaveCount(0);
    });
  });
});

test.describe('layout on small screens', () => {
  test('keeps the page within the viewport and scrolls the tables inside', async ({
    page,
    browser,
  }) => {
    await page.goto(`/deployments/${await prod(browser)}`);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      for (const panel of ['#services', '#hist']) {
        const scroller = page.locator(`${panel} .overflow-x-auto`);
        expect(await scroller.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
          true,
        );
      }
    }
  });
});
