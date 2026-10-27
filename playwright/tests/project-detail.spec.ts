import type { Browser, Page } from '@playwright/test';

import { stateFile, SUPERADMIN_STATE } from '../global-setup';
import { expectBreadcrumb, expectItemHeader, expectLocation } from '../item-header';
import { listProjects } from '../projects-api';
import { seededEmail } from '../users-api';
import { horizontalOverflow, isWide } from '../viewport';
import { actionControls } from './row-actions';
import { expect, test } from './scratch-project';

/** Id of a seeded project by name, looked up as the superadmin so any role's spec can use it. */
async function projectId(browser: Browser, name: string): Promise<number> {
  const context = await browser.newContext({ storageState: SUPERADMIN_STATE });
  try {
    const found = (await listProjects(context.request, name)).find(([, listed]) => listed === name);
    if (!found) {
      throw new Error(`seeded project ${name} is missing`);
    }
    return found[0];
  } finally {
    await context.close();
  }
}

function tabs(page: Page) {
  return page.locator('nav[aria-label="Sections"] a');
}

function deploymentNames(page: Page) {
  return page.locator('#deployments-table [data-fragment="deployment-row"] th[scope="row"] a');
}

test.describe('superadmin', () => {
  test('shows the header, the four tabs and the deployments with their actions', async ({
    page,
    browser,
  }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto('/projects');
    await page.locator('[data-fragment="project-row"] th a', { hasText: /^Fancy Proj\. Name$/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${fancy}$`));
    await expectItemHeader(page, 'project', 'Fancy Proj. Name');
    await expectLocation(page, 'project', {});
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
      /^2 deployments · \d+ members$/,
    );
    await expectBreadcrumb(page, ['Projects', ['Fancy Proj. Name', 'Project']]);
    await expect(tabs(page)).toHaveText([
      'Deployments',
      'Members and accessSuperadmin',
      'Activity',
      'SettingsSuperadmin',
    ]);
    await expect(tabs(page).first()).toHaveAttribute('aria-current', 'page');

    await expect(page.locator('#deployments-table th[scope="col"]')).toHaveText([
      'Name',
      'Root path',
      'Files',
      'Last deploy',
      'State flags',
      'Actions',
    ]);
    await expect(deploymentNames(page)).toHaveText(['prod', 'staging']);
    const prod = page.locator('[data-fragment="deployment-row"]', { hasText: 'prod' });
    await expect(prod).toContainText('/opt/fancy/prod');
    await expect(actionControls(prod, 'open').first()).toHaveAttribute(
      'href',
      /^\/deployments\/\d+$/,
    );
    await expect(page.getByRole('link', { name: 'New deployment' })).toHaveAttribute(
      'href',
      `/projects/${fancy}/deployments/new`,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('moves between the tabs', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}`);

    await tabs(page).filter({ hasText: 'Activity' }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${fancy}/activity$`));
    await expect(tabs(page).filter({ hasText: 'Activity' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.locator('[data-fragment="activity-table"]')).toBeVisible();

    await tabs(page).filter({ hasText: 'Members and access' }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${fancy}/access$`));
    await expect(page.locator('[data-fragment="access-list"]')).toBeVisible();

    await tabs(page).filter({ hasText: 'Settings' }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${fancy}/settings$`));
    await expect(page.locator('[data-fragment="project-rename"]')).toBeVisible();

    await tabs(page).filter({ hasText: 'Deployments' }).click();
    await expect(page.locator('[data-fragment="deployments-table"]')).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('lists who has access, direct and inherited', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}/access`);
    const list = page.locator('#access-list');
    await expect(list).toContainText('Users with access to project Fancy Proj. Name');
    await expect(list.locator('th[scope="col"]')).toHaveText(['User', 'Group', 'Access', 'Actions']);
    const row = (name: string) =>
      list.locator('tbody tr').filter({
        has: page.getByRole('rowheader', { name: `${name} ${seededEmail(name)}`, exact: true }),
      });
    await expect(row('project_admin')).toContainText('Direct');
    await expect(row('deployment_admin')).toContainText('Inherited');
    await expect(row('deployment_admin')).toContainText('from prod (Deployment)');
    await expect(row('env_scoped_admin')).toContainText('from mysql/.env.db (Environment)');
    await expect(row('root')).toHaveCount(0);
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
      /^2 deployments · \d+ members$/,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('shows the empty state and the creation in the activity', async ({ page, scratch }) => {
    await page.goto(`/projects/${scratch.id}`);
    await expectItemHeader(page, 'project', scratch.name);
    await expect(page.locator('[data-fragment="page-header"] [data-status="drift"]')).toHaveText(
      'Empty',
    );
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
      '0 deployments · 0 members',
    );
    const empty = page.locator('#deployments-table [data-fragment="empty-state"]');
    await expect(empty).toContainText('No deployments yet');
    await expect(empty.getByRole('link', { name: 'New deployment' })).toHaveAttribute(
      'href',
      `/projects/${scratch.id}/deployments/new`,
    );
    await expect(page.getByRole('link', { name: 'New deployment' })).toHaveCount(2);

    await page.goto(`/projects/${scratch.id}/activity`);
    const panel = page.locator('[data-fragment="activity-table"]');
    await expect(panel.locator('th[scope="col"]')).toHaveText([
      'Time',
      'Actor',
      'Action',
      'Object',
      'Result',
    ]);
    const entry = panel.locator('tbody tr', { hasText: scratch.name });
    await expect(entry).toHaveCount(1);
    await expect(entry).toContainText('root');
    await expect(entry).toContainText('project.create');
    await expect(entry.locator('[data-status="success"]')).toBeVisible();
  });

  test('refuses bad and duplicate names, then renames', async ({ page, scratch }) => {
    await page.goto(`/projects/${scratch.id}/settings`);
    const form = page.locator('#project-rename');
    await expect(form.locator('#rn')).toHaveValue(scratch.name);

    await form.locator('#rn').fill('Bad Name!');
    await form.getByRole('button', { name: 'Save name' }).click();
    await expect(form.locator('#rn-e[role="alert"]')).toContainText('Use 3 to 100 characters');
    await expect(form.locator('#rn')).toHaveAttribute('aria-invalid', 'true');
    await expect(form.locator('#rn')).toHaveValue('Bad Name!');

    await form.locator('#rn').fill('Fancy Proj. Name');
    await form.getByRole('button', { name: 'Save name' }).click();
    await expect(form.locator('#rn-e[role="alert"]')).toContainText('already exists');

    const renamed = `${scratch.name}-b`;
    await form.locator('#rn').fill(renamed);
    await form.getByRole('button', { name: 'Save name' }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${scratch.id}/settings\\?renamed=true$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Project renamed');
    await expectItemHeader(page, 'project', renamed);
    await expectBreadcrumb(page, ['Projects', [renamed, 'Project']]);
    await expect(page.locator('#project-rename #rn')).toHaveValue(renamed);
  });

  test('keeps the extra notification addresses, refuses a bad one and keeps each address once', async ({
    page,
    scratch,
  }) => {
    await page.goto(`/projects/${scratch.id}/settings`);
    const panel = page.locator('#project-addresses');
    await expect(panel.locator('h2')).toHaveText('Notification addresses');
    await expect(panel.locator('label[for="xa"]')).toHaveText('Extra addresses, one per line');
    await expect(panel.locator('#xa')).toHaveValue('');
    const titles = await page.locator('main section h2').allTextContents();
    expect(titles.indexOf('Rename')).toBeLessThan(titles.indexOf('Notification addresses'));
    expect(titles.indexOf('Notification addresses')).toBeLessThan(titles.indexOf('Delete project'));

    await panel.locator('#xa').fill('ops@example.test\nnot-an-address');
    await panel.getByRole('button', { name: 'Save addresses' }).click();
    await expect(panel.locator('#xa-e[role="alert"]')).toHaveText(
      'not-an-address is not a valid address.',
    );
    await expect(panel.locator('#xa')).toHaveAttribute('aria-invalid', 'true');
    await expect(panel.locator('#xa')).toHaveValue('ops@example.test\nnot-an-address');

    await panel.locator('#xa').fill('Ops@Example.test\n\nops@example.test\nteam@example.test');
    await panel.getByRole('button', { name: 'Save addresses' }).click();
    await expect(panel.locator('[data-fragment="toast"]')).toContainText('Addresses saved');
    await expect(panel.locator('#xa')).toHaveValue('Ops@Example.test\nteam@example.test');
    await page.reload();
    await expect(page.locator('#xa')).toHaveValue('Ops@Example.test\nteam@example.test');
    await page.locator('#project-addresses').getByRole('button', { name: 'Save addresses' }).click();
    await expect(page.locator('#project-addresses [data-fragment="toast"]')).toContainText(
      'Nothing to save',
    );

    await page.goto('/audit?action=project.notification_addresses');
    const entry = page
      .locator('#audit-results tr[data-fragment="audit-row"]', { hasText: scratch.name })
      .first();
    await expect(entry).toContainText('project.notification_addresses');
    await expect(entry.locator('xpath=following-sibling::tr[1]')).toContainText(
      'team@example.test',
    );

    await page.goto(`/projects/${scratch.id}/settings`);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      const button = page.locator('#project-addresses').getByRole('button', {
        name: 'Save addresses',
      });
      expect((await button.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
    }
  });

  test('confirms by name before deleting and then leaves for the list', async ({
    page,
    scratch,
  }) => {
    await page.goto(`/projects/${scratch.id}/settings`);
    await page.getByRole('button', { name: /Delete project/ }).click();
    const dialog = page.locator('#delete-project[open]');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('#delete-project-t')).toHaveText(`Delete project ${scratch.name}`);
    await expect(dialog).toContainText('its 0 deployments');
    await expect(dialog).toContainText('This cannot be undone.');

    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('#delete-project[open]')).toHaveCount(0);

    await page.getByRole('button', { name: /Delete project/ }).click();
    await dialog.locator('#dn').fill('not-the-name');
    await dialog.getByRole('button', { name: 'Delete project' }).click();
    await expect(dialog.locator('#dn-e[role="alert"]')).toContainText(
      'Type the project name exactly to confirm.',
    );
    await expect(dialog.locator('#dn')).toHaveValue('not-the-name');
    expect((await listProjects(page.request, scratch.name)).length).toBe(1);

    await dialog.locator('#dn').fill(scratch.name);
    await dialog.getByRole('button', { name: 'Delete project' }).click();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.locator('#projects-table')).not.toContainText(scratch.name);
    expect((await page.request.get(`/projects/${scratch.id}`)).status()).toBe(404);
  });

  test('refreshes the deployments panel when a deploy finishes', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}`);
    const refreshed = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `/projects/${fancy}/deployments` &&
        response.status() === 200,
    );
    await page.evaluate(() =>
      document.body.dispatchEvent(new CustomEvent('deploy-finished', { bubbles: true })),
    );
    await refreshed;
    await expect(deploymentNames(page)).toHaveText(['prod', 'staging']);
  });

  test('keeps the deployments and shows a toast when the refresh fails', async ({
    page,
    browser,
  }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}`);
    await page.route(`**/projects/${fancy}/deployments`, (route) => route.abort());
    await page.evaluate(() =>
      document.body.dispatchEvent(new CustomEvent('deploy-finished', { bubbles: true })),
    );
    await expect(page.locator('[data-fragment="toast"][role="alert"]')).toBeVisible();
    await expect(deploymentNames(page)).toHaveText(['prod', 'staging']);
  });

  test('answers 404 for an unknown project', async ({ page }) => {
    const response = await page.goto('/projects/999999');
    expect(response?.status()).toBe(404);
    await expect(page.locator('main')).toContainText('Not found');
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('sees deployments and activity without structure actions', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}`);
    await expect(tabs(page)).toHaveText(['Deployments', 'Activity']);
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText('2 deployments');
    await expect(deploymentNames(page)).toHaveText(['prod', 'staging']);
    await expect(page.getByRole('link', { name: 'New deployment' })).toHaveCount(0);
    await expect(page.locator('[data-fragment="page-header"] [data-js="dialog-open"]')).toHaveCount(
      0,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('is refused the superadmin tabs', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    for (const tab of ['access', 'members', 'settings']) {
      const response = await page.goto(`/projects/${fancy}/${tab}`);
      expect(response?.status()).toBe(403);
      await expect(page.locator('main')).toContainText('No access');
    }
  });

  test('lists the activity in scope', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}/activity`);
    await expect(page.locator('[data-fragment="activity-table"]')).toBeVisible();
    await expect(page.locator('main')).not.toContainText('Settings');
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('reads deployments with no edit actions and no activity tab', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}`);
    await expect(tabs(page)).toHaveText(['Deployments']);
    await expect(deploymentNames(page)).toHaveText(['prod', 'staging']);
    await expect(page.getByRole('link', { name: 'New deployment' })).toHaveCount(0);
  });

  test('is refused the activity tab', async ({ page, browser }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    const response = await page.goto(`/projects/${fancy}/activity`);
    expect(response?.status()).toBe(403);
    await expect(page.locator('main')).toContainText('No access');
  });

  test('cannot open a project outside the grants', async ({ page, browser }) => {
    const billing = await projectId(browser, 'billing');
    const response = await page.goto(`/projects/${billing}`);
    expect(response?.status()).toBe(404);
    await expect(page.locator('main')).toContainText('Not found');
    await expect(page.locator('main')).not.toContainText('billing');
  });
});

test.describe('scoped users', () => {
  test.describe('deployment grant', () => {
    test.use({ storageState: stateFile('deploymentAdmin') });

    test('sees only the granted deployment and is told so', async ({ page, browser }) => {
      const fancy = await projectId(browser, 'Fancy Proj. Name');
      await page.goto(`/projects/${fancy}`);
      await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
        'Your access is limited to one deployment of this project.',
      );
      await expect(page.locator('[role="note"]')).toHaveText(
        'You have access to prod only. Other deployments of this project are not listed.',
      );
      await expect(deploymentNames(page)).toHaveText(['prod']);
      await expect(page.locator('[data-fragment="page-header"] [data-status]')).toHaveCount(0);
      await expect(page.locator('main')).not.toContainText('staging');
      await expect(page.locator('main')).not.toContainText('billing');
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    });
  });

  test.describe('environment grants', () => {
    test.use({ storageState: stateFile('envScopedViewer') });

    test('sees the parent deployment by name only', async ({ page, browser }) => {
      const fancy = await projectId(browser, 'Fancy Proj. Name');
      await page.goto(`/projects/${fancy}`);
      await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
        'Your access is limited to selected files of this project.',
      );
      await expect(page.locator('[role="note"]')).toContainText('You have access to .env.db only.');
      await expect(deploymentNames(page)).toHaveText(['prod']);
      const row = page.locator('[data-fragment="deployment-row"]');
      await expect(row).toContainText('Limited access');
      await expect(row).not.toContainText('/opt/fancy');
      await expect(page.locator('main')).not.toContainText('.env.web');
      await expect(page.locator('main')).not.toContainText('mysql');
    });
  });
});

test.describe('layout on small screens', () => {
  test('keeps the page within the viewport and scrolls the table inside', async ({
    page,
    browser,
  }) => {
    const fancy = await projectId(browser, 'Fancy Proj. Name');
    await page.goto(`/projects/${fancy}`);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      const scroller = page.locator('#deployments-table .overflow-x-auto');
      expect(await scroller.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);
    }
    expect(await tabs(page).count()).toBe(4);
  });
});
