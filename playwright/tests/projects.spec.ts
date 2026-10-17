import { expect, test, type Page, type Route } from '@playwright/test';

import { stateFile } from '../global-setup';
import { deleteProject, listProjects } from '../projects-api';

const WIDE_BREAKPOINT = 768;

// The panel the server answers when the list cannot be loaded, with the same swap wiring as the
// real section; the 503 itself is produced here because the stack has no way to fail on demand.
const FAILED_LOAD_PANEL =
  '<section data-fragment="projects-table" id="projects-table" aria-label="Projects" hx-get="/projects/table" hx-include="#ps" hx-swap="outerHTML">' +
  '<div role="alert" data-fragment="inline-error"><p>Could not load projects</p>' +
  '<p>The server did not respond. Your data is unchanged.</p>' +
  '<button type="button" hx-get="/projects/table" hx-include="#ps" hx-target="#projects-table" hx-swap="outerHTML" hx-status:503="swap:outerHTML target:#projects-table">Retry</button></div></section>';

function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

function rowNames(page: Page) {
  return page.locator('[data-fragment="project-row"] th[scope="row"] a');
}

test.describe('superadmin', () => {
  test('lists every project with counts, members and the create action', async ({ page }) => {
    await page.goto('/projects');
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('Projects');
    await expect(page.getByText('All projects.')).toBeVisible();
    const table = page.locator('#projects-table');
    await expect(table.locator('th[scope="col"]')).toHaveText([
      'Project',
      'Deployments',
      'Last deploy',
      'Members',
      'Attention',
    ]);
    await expect(rowNames(page).filter({ hasText: /^rentals$/ })).toHaveAttribute(
      'href',
      /^\/projects\/\d+$/,
    );
    await expect(rowNames(page).filter({ hasText: /^billing$/ })).toBeVisible();
    await expect(
      page.locator('[data-fragment="project-row"]', { hasText: 'rentals' }).locator('td').first(),
    ).toHaveText('2');
    await expect(page.locator('[data-js="dialog-open"][data-target="project-new"]')).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('searches by name and says so when nothing matches', async ({ page }) => {
    await page.goto('/projects');
    const search = page.locator('#ps');
    await search.fill('rent');
    await expect(rowNames(page)).toHaveText(['rentals']);
    await search.fill('no-such-project');
    await expect(page.locator('#projects-table [data-fragment="empty-state"]')).toContainText(
      'No projects match',
    );
    await search.fill('');
    await expect(rowNames(page)).not.toHaveCount(0);
    await page.goto('/projects?q=bill');
    await expect(page.locator('#ps')).toHaveValue('bill');
    await expect(rowNames(page)).toHaveText(['billing']);
  });

  test('opens and cancels the new project dialog', async ({ page }) => {
    await page.goto('/projects');
    await page.locator('[data-js="dialog-open"][data-target="project-new"]').first().click();
    const dialog = page.locator('#project-new[open]');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('Superadmin only')).toBeVisible();
    await expect(dialog.locator('#pn')).toBeFocused({ timeout: 2000 }).catch(() => undefined);
    await expect(dialog.getByText('Lowercase letters, numbers and dashes.')).toBeVisible();
    await dialog.locator('[data-js="dialog-close"]').last().click();
    await expect(page.locator('#project-new[open]')).toHaveCount(0);
  });

  test('refuses bad and duplicate names, then creates a project', async ({ page }) => {
    await page.goto('/projects');
    await page.locator('[data-js="dialog-open"][data-target="project-new"]').first().click();
    const dialog = page.locator('#project-new[open]');

    await dialog.locator('#pn').fill('Bad Name');
    await dialog.getByRole('button', { name: 'Create project' }).click();
    await expect(dialog.locator('#pn-e[role="alert"]')).toContainText('lowercase letters');
    await expect(dialog.locator('#pn')).toHaveAttribute('aria-invalid', 'true');
    await expect(dialog.locator('#pn')).toHaveValue('Bad Name');

    await dialog.locator('#pn').fill('rentals');
    await dialog.getByRole('button', { name: 'Create project' }).click();
    await expect(dialog.locator('#pn-e[role="alert"]')).toContainText('already exists');

    const name = `e2e-${Date.now()}-${test.info().project.name}`;
    try {
      await dialog.locator('#pn').fill(name);
      await dialog.getByRole('button', { name: 'Create project' }).click();
      await expect(page.locator('#project-new[open]')).toHaveCount(0);
      await expect(rowNames(page).filter({ hasText: new RegExp(`^${name}$`) })).toBeVisible();

      await page.locator('[data-js="dialog-open"][data-target="project-new"]').first().click();
      await expect(page.locator('#project-new #pn')).toHaveValue('');
      await expect(page.locator('#project-new #pn-e')).toHaveCount(0);
    } finally {
      for (const [id, listed] of await listProjects(page.request, name)) {
        if (listed === name) {
          await deleteProject(page.request, id, name);
        }
      }
    }
  });

  test('swaps in the error panel on a failed load and recovers with Retry', async ({ page }) => {
    await page.goto('/projects');
    const failure = async (route: Route) =>
      route.fulfill({
        status: 503,
        contentType: 'text/html',
        body: FAILED_LOAD_PANEL,
      });
    await page.route('**/projects/table**', failure);
    await page.locator('#ps').fill('rent');
    const panel = page.locator('#projects-table [data-fragment="inline-error"]');
    await expect(panel).toContainText('Could not load projects');
    await expect(panel).toHaveAttribute('role', 'alert');
    await expect(page.locator('[data-fragment="toast"][role="alert"]')).toBeVisible();

    await page.unroute('**/projects/table**', failure);
    await panel.getByRole('button', { name: 'Retry' }).click();
    await expect(page.locator('#projects-table table')).toBeVisible();
    await expect(rowNames(page)).toHaveText(['rentals']);
  });

  test('keeps the list and shows a toast when the refresh fails', async ({ page }) => {
    await page.goto('/projects');
    await page.route('**/projects/table**', (route) => route.abort());
    await page.locator('#ps').fill('rent');
    await expect(page.locator('[data-fragment="toast"][role="alert"]')).toBeVisible();
    await expect(page.locator('#projects-table table')).toBeVisible();
    await expect(rowNames(page).filter({ hasText: /^billing$/ })).toBeVisible();
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('sees every granted project without members or create', async ({ page }) => {
    await page.goto('/projects');
    await expect(page.getByText('Projects you have access to.')).toBeVisible();
    await expect(rowNames(page)).toHaveText(['billing', 'rentals']);
    await expect(page.locator('#projects-table th[scope="col"]')).toHaveText([
      'Project',
      'Deployments',
      'Last deploy',
      'Attention',
    ]);
    await expect(page.locator('[data-js="dialog-open"][data-target="project-new"]')).toHaveCount(0);
    await expect(page.locator('#project-new')).toHaveCount(0);
  });

  test('is refused when posting a new project', async ({ page }) => {
    await page.goto('/projects');
    const csrf = (await page.context().cookies()).find((c) => c.name === 'csrftoken')?.value ?? '';
    const response = await page.request.post('/projects', {
      form: { name: 'sneaky' },
      headers: { 'HX-Request': 'true', 'X-CSRFToken': csrf },
    });
    expect(response.status()).toBe(403);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('sees only the granted project and no edit actions', async ({ page }) => {
    await page.goto('/projects');
    await expect(rowNames(page)).toHaveText(['rentals']);
    await expect(page.locator('[data-js="dialog-open"]').filter({ hasText: 'New project' })).toHaveCount(0);
    await expect(page.locator('#project-new')).toHaveCount(0);
  });
});

test.describe('scoped users', () => {
  test.describe('deployment grant', () => {
    test.use({ storageState: stateFile('deploymentAdmin') });

    test('sees the parent project limited to the granted deployment', async ({ page }) => {
      await page.goto('/projects');
      await expect(rowNames(page)).toHaveText(['rentals']);
      const row = page.locator('[data-fragment="project-row"]');
      await expect(row).toContainText('Access limited to prod');
      await expect(row).toContainText('of your scope');
      await expect(page.locator('main')).not.toContainText('billing');
      await expect(page.locator('main')).not.toContainText('staging');
    });
  });

  test.describe('environment grants', () => {
    test.use({ storageState: stateFile('envScopedViewer') });

    test('sees projects limited to the granted environments only', async ({ page }) => {
      await page.goto('/projects');
      await expect(rowNames(page)).toHaveText(['billing', 'rentals']);
      await expect(page.locator('[data-fragment="project-row"]', { hasText: 'rentals' })).toContainText(
        'Access limited to .env.db',
      );
      await expect(page.locator('[data-fragment="project-row"]', { hasText: 'billing' })).toContainText(
        'Access limited to .env.api',
      );
      await expect(page.locator('main')).not.toContainText('.env.web');
      await expect(page.locator('main')).not.toContainText('worker');
    });
  });
});

test.describe('no access', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('explains how to get access', async ({ page }) => {
    await page.goto('/projects');
    const empty = page.locator('#projects-table [data-fragment="empty-state"]');
    await expect(empty).toContainText('No projects assigned');
    await expect(empty).toContainText('Ask a superadmin to grant you access');
    await expect(page.locator('[data-js="dialog-open"][data-target="project-new"]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      await expect(page.locator('[data-nav]')).toBeHidden();
    }
  });
});
