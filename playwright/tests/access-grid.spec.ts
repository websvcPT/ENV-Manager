import type { Locator, Page } from '@playwright/test';

import { stateFile } from '../global-setup';
import { csrfHeaders } from '../projects-api';
import type { CreatedUser } from '../users-api';
import {
  expect,
  horizontalOverflow,
  isWide,
  signedInAs,
  test,
  TOUCH_TARGET,
  type Scratch,
} from './scratch-access';

/** The grid narrowed to the scratch project, whose rows no other spec touches. */
async function openGrid(page: Page, scratch: Scratch): Promise<void> {
  await page.goto(`/access/grid?project=${scratch.projectId}`);
  await expect(page.locator('#grid-table table')).toBeVisible();
}

function cell(page: Page, user: CreatedUser, node: string): Locator {
  return page.locator(`#cell-${user.id}-${node}`);
}

function box(page: Page, user: CreatedUser, node: string): Locator {
  return page.locator(`#grant-${user.id}-${node}`);
}

function rowOf(page: Page, node: string): Locator {
  return page.locator(`tr[data-node="${node}"]`);
}

test.describe('superadmin: the access grid', () => {
  test('draws the tree against the users with a control only where a grant can be set', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    const admin = await makeUser('admin');
    await openGrid(page, scratch);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('Access grid');
    await expect(
      page.getByText('Rows are projects, deployments, services and environments.'),
    ).toBeVisible();
    await expect(page.locator('nav[aria-label="Primary"] a[aria-current="page"]').first()).toHaveText(
      /Users/,
    );
    await expect(page.locator('nav[aria-label="Breadcrumb"] [aria-current="page"]')).toHaveText(
      'Access grid',
    );
    const tabs = page.locator('nav[aria-label="Sections"]');
    await expect(tabs.locator('a[aria-current="page"]')).toHaveText(/Access grid/);
    await expect(tabs.locator('a[href="/users"]')).not.toHaveAttribute('aria-current', 'page');
    await expect(tabs.getByText('Superadmin')).toBeVisible();
    const legend = page.locator('ul[aria-label="Legend"]');
    await expect(legend.locator('li')).toHaveText([
      'Direct grant',
      'Inherited, not editable',
      'Service result',
    ]);
    await expect(page.locator('#g-g option')).toHaveText(['All groups', 'Admin', 'Viewer']);
    await expect(page.locator('#g-p')).toHaveValue(String(scratch.projectId));

    const columns = page.locator('#grid-table thead th[scope="col"]');
    await expect(columns.first()).toHaveText('Item');
    await expect(page.locator(`#grid-user-${viewer.id}`)).toContainText(viewer.name);
    await expect(page.locator(`#grid-user-${viewer.id}`)).toContainText('Viewer');
    await expect(page.locator(`#grid-user-${admin.id}`)).toContainText('Admin');
    await expect(columns.filter({ hasText: /^root/ })).toHaveCount(0);

    const nodes = [
      `project-${scratch.projectId}`,
      `deployment-${scratch.depA}`,
      `service-${scratch.serviceA}`,
      `environment-${scratch.envA1}`,
      `environment-${scratch.envA2}`,
      `deployment-${scratch.depB}`,
      `service-${scratch.serviceB}`,
      `environment-${scratch.envB}`,
    ];
    await expect(page.locator('#grid-table tbody tr[data-node]')).toHaveCount(nodes.length);
    for (const node of nodes) {
      await expect(rowOf(page, node)).toBeVisible();
    }
    await expect(rowOf(page, nodes[0]).locator('th')).toContainText('Project');
    await expect(rowOf(page, nodes[0]).locator('th')).toContainText(scratch.projectName);
    await expect(rowOf(page, nodes[1]).locator('th')).toContainText('Deployment');
    await expect(rowOf(page, nodes[2]).locator('th')).toContainText('Service');
    await expect(rowOf(page, nodes[3]).locator('th')).toContainText('Environment');
    await expect(rowOf(page, nodes[3]).locator('th')).toContainText('.env.cache');
    await expect(rowOf(page, nodes[1])).toHaveAttribute('data-parent', nodes[0]);
    await expect(rowOf(page, nodes[3])).toHaveAttribute('data-parent', nodes[2]);

    for (const node of [nodes[0], nodes[1], nodes[3], nodes[4], nodes[5], nodes[7]]) {
      await expect(box(page, viewer, node)).not.toBeChecked();
      await expect(cell(page, viewer, node)).toHaveAttribute('data-cell', 'none');
      await expect(cell(page, viewer, node)).toContainText('None');
    }
    for (const node of [nodes[2], nodes[6]]) {
      await expect(cell(page, viewer, node)).toHaveAttribute('data-cell', 'result');
      await expect(cell(page, viewer, node)).toContainText('No access');
      await expect(cell(page, viewer, node).locator('input')).toHaveCount(0);
    }
    await expect(page.getByText('Grants exist at project, deployment and environment rows.')).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('a project grant makes everything below inherited, shows the result and applies at once', async ({
    page,
    browser,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    const other = await makeUser('viewer');
    await openGrid(page, scratch);
    await page.evaluate(() => {
      const events: string[] = [];
      (window as unknown as { grantEvents: string[] }).grantEvents = events;
      document.addEventListener('grants-changed', () => events.push('grants-changed'));
    });
    const project = `project-${scratch.projectId}`;
    await box(page, viewer, project).click();
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'direct');
    await expect(box(page, viewer, project)).toBeChecked();
    await expect(cell(page, viewer, project)).toContainText('Direct');
    await expect(cell(page, viewer, project)).toContainText('+ future items');
    for (const node of [
      `deployment-${scratch.depA}`,
      `environment-${scratch.envA1}`,
      `environment-${scratch.envA2}`,
      `deployment-${scratch.depB}`,
      `environment-${scratch.envB}`,
    ]) {
      await expect(cell(page, viewer, node)).toHaveAttribute('data-cell', 'inherited');
      await expect(cell(page, viewer, node)).toContainText(`from ${scratch.projectName}`);
      await expect(cell(page, viewer, node).locator('input')).toHaveCount(0);
    }
    await expect(cell(page, viewer, `service-${scratch.serviceA}`)).toContainText('Sees 2 of 2');
    await expect(cell(page, viewer, `service-${scratch.serviceB}`)).toContainText('Sees 1 of 1');
    await expect(cell(page, other, project)).toHaveAttribute('data-cell', 'none');
    await expect(cell(page, other, `environment-${scratch.envA1}`)).toHaveAttribute(
      'data-cell',
      'none',
    );
    expect(await page.evaluate(() => (window as never as { grantEvents: string[] }).grantEvents)).toEqual([
      'grants-changed',
    ]);

    const person = await signedInAs(browser, viewer);
    try {
      await expect(person.locator('#projects-table')).toContainText(scratch.projectName);
      await box(page, viewer, project).click();
      await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'none');
      await expect(box(page, viewer, project)).not.toBeChecked();
      for (const node of [
        `deployment-${scratch.depA}`,
        `environment-${scratch.envA1}`,
        `environment-${scratch.envB}`,
      ]) {
        await expect(cell(page, viewer, node)).toHaveAttribute('data-cell', 'none');
        await expect(box(page, viewer, node)).toBeVisible();
      }
      await expect(cell(page, viewer, `service-${scratch.serviceA}`)).toContainText('No access');
      await person.reload();
      await expect(person.locator('#projects-table')).not.toContainText(scratch.projectName);
    } finally {
      await person.context().close();
    }
  });

  test('a deployment grant passes down with the deployment as source', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const admin = await makeUser('admin');
    await openGrid(page, scratch);
    const deployment = `deployment-${scratch.depA}`;
    await box(page, admin, deployment).click();
    await expect(cell(page, admin, deployment)).toHaveAttribute('data-cell', 'direct');
    await expect(cell(page, admin, deployment)).toContainText('+ future items');
    await expect(cell(page, admin, `environment-${scratch.envA1}`)).toContainText('from dep-a');
    await expect(cell(page, admin, `environment-${scratch.envA2}`)).toContainText('from dep-a');
    await expect(cell(page, admin, `service-${scratch.serviceA}`)).toContainText('Sees 2 of 2');
    await expect(cell(page, admin, `project-${scratch.projectId}`)).toHaveAttribute(
      'data-cell',
      'none',
    );
    await expect(cell(page, admin, `deployment-${scratch.depB}`)).toHaveAttribute(
      'data-cell',
      'none',
    );
    await expect(cell(page, admin, `service-${scratch.serviceB}`)).toContainText('No access');
  });

  test('an environment grant shows a partial service result and no future items', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    await openGrid(page, scratch);
    const environment = `environment-${scratch.envA1}`;
    await box(page, viewer, environment).click();
    await expect(cell(page, viewer, environment)).toHaveAttribute('data-cell', 'direct');
    await expect(cell(page, viewer, environment)).not.toContainText('future');
    await expect(cell(page, viewer, `service-${scratch.serviceA}`)).toContainText('Sees 1 of 2');
    await expect(cell(page, viewer, `environment-${scratch.envA2}`)).toHaveAttribute(
      'data-cell',
      'none',
    );
    await expect(cell(page, viewer, `deployment-${scratch.depA}`)).toHaveAttribute(
      'data-cell',
      'none',
    );
    await box(page, viewer, `environment-${scratch.envA2}`).click();
    await expect(cell(page, viewer, `service-${scratch.serviceA}`)).toContainText('Sees 2 of 2');
    await box(page, viewer, environment).click();
    await expect(cell(page, viewer, `service-${scratch.serviceA}`)).toContainText('Sees 1 of 2');
  });

  test('shows a grant held again below a project grant as direct and removable', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer', [
      `project:${scratch.projectId}`,
      `environment:${scratch.envA1}`,
    ]);
    await openGrid(page, scratch);
    const environment = `environment-${scratch.envA1}`;
    await expect(cell(page, viewer, environment)).toHaveAttribute('data-cell', 'direct');
    await expect(cell(page, viewer, environment)).toContainText(
      `Also included through ${scratch.projectName}`,
    );
    await expect(cell(page, viewer, `environment-${scratch.envA2}`)).toHaveAttribute(
      'data-cell',
      'inherited',
    );
    await box(page, viewer, environment).click();
    await expect(cell(page, viewer, environment)).toHaveAttribute('data-cell', 'inherited');
    await expect(cell(page, viewer, environment)).toContainText(`from ${scratch.projectName}`);
  });

  test('keeps the collapsed rows and the focus when a cell changes', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    await openGrid(page, scratch);
    const project = `project-${scratch.projectId}`;
    const toggle = rowOf(page, project).locator('[data-js="tree-toggle"]');
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(rowOf(page, `deployment-${scratch.depA}`)).toBeHidden();
    await expect(rowOf(page, `environment-${scratch.envB}`)).toBeHidden();
    await box(page, viewer, project).click();
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'direct');
    await expect(rowOf(page, `deployment-${scratch.depA}`)).toBeHidden();
    await expect(rowOf(page, `environment-${scratch.envB}`)).toBeHidden();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await toggle.click();
    await expect(rowOf(page, `environment-${scratch.envB}`)).toBeVisible();
    await expect(cell(page, viewer, `environment-${scratch.envB}`)).toHaveAttribute(
      'data-cell',
      'inherited',
    );
    const deployment = rowOf(page, `deployment-${scratch.depA}`).locator('[data-js="tree-toggle"]');
    await deployment.click();
    await expect(rowOf(page, `service-${scratch.serviceA}`)).toBeHidden();
    await expect(rowOf(page, `environment-${scratch.envA1}`)).toBeHidden();
    await expect(rowOf(page, `deployment-${scratch.depB}`)).toBeVisible();
    await box(page, viewer, project).focus();
    await box(page, viewer, project).press('Space');
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'none');
    await expect(box(page, viewer, project)).toBeFocused();
    await expect(rowOf(page, `environment-${scratch.envA1}`)).toBeHidden();
  });

  test('filters the columns by group and keeps the view in the address', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    const admin = await makeUser('admin');
    await page.goto('/access/grid');
    await expect(page.locator(`#grid-user-${viewer.id}`)).toBeVisible();
    await expect(page.locator(`#grid-user-${admin.id}`)).toBeVisible();
    await page.locator('#g-g').focus();
    await page.locator('#g-g').selectOption('viewer');
    await expect(page).toHaveURL(/\/access\/grid\?group=viewer$/);
    await expect(page.locator(`#grid-user-${admin.id}`)).toHaveCount(0);
    await expect(page.locator(`#grid-user-${viewer.id}`)).toBeVisible();
    await expect(page.locator('#g-g')).toBeFocused();
    await page.locator('#g-p').selectOption(String(scratch.projectId));
    await expect(page).toHaveURL(new RegExp(`\\?group=viewer&project=${scratch.projectId}$`));
    await expect(page.locator('#grid-table tbody tr[data-node]')).toHaveCount(8);
    await expect(rowOf(page, `project-${scratch.projectId}`)).toBeVisible();
    await page.reload();
    await expect(page.locator('#g-g')).toHaveValue('viewer');
    await expect(page.locator('#g-p')).toHaveValue(String(scratch.projectId));
    await expect(page.locator(`#grid-user-${admin.id}`)).toHaveCount(0);
    await expect(page.locator('#grid-table tbody tr[data-node]')).toHaveCount(8);
    await page.locator('#g-g').selectOption('admin');
    await expect(page.locator(`#grid-user-${admin.id}`)).toBeVisible();
    await expect(page.locator(`#grid-user-${viewer.id}`)).toHaveCount(0);
    await page.locator('#g-g').selectOption('');
    await page.locator('#g-p').selectOption('');
    await expect(page).toHaveURL(/\/access\/grid$/);
    await expect(page.locator(`#grid-user-${viewer.id}`)).toBeVisible();
    await expect(page.locator(`#grid-user-${admin.id}`)).toBeVisible();
  });

  test('refuses a cell from an out of date page and shows the current state', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer');
    await openGrid(page, scratch);
    const project = `project-${scratch.projectId}`;
    const saved = await request.post(`/users/${viewer.id}/grants`, {
      form: { grant: `project:${scratch.projectId}`, base: '' },
      headers: await csrfHeaders(request),
    });
    expect(saved.ok()).toBe(true);
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'none');
    await box(page, viewer, project).click();
    await expect(cell(page, viewer, project).locator('[role="alert"]')).toContainText(
      'changed since the page was loaded',
    );
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'direct');
    await expect(box(page, viewer, project)).toBeChecked();
    await expect(cell(page, viewer, `environment-${scratch.envA1}`)).toHaveAttribute(
      'data-cell',
      'inherited',
    );
    await expect(cell(page, viewer, `service-${scratch.serviceA}`)).toContainText('Sees 2 of 2');
    const detail = await request.get(`/users/${viewer.id}`);
    expect(await detail.text()).toMatch(new RegExp(`id="grant-project-${scratch.projectId}" [^>]*checked`));
    await box(page, viewer, project).click();
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'none');
    await expect(cell(page, viewer, project).locator('[role="alert"]')).toHaveCount(0);
  });

  test('refuses what the page never offers and writes nothing', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer', [`project:${scratch.projectId}`]);
    await openGrid(page, scratch);
    const headers = await csrfHeaders(request);
    const post = async (form: Record<string, string>) =>
      request.post('/access/grid/toggle', { form, headers });
    const included = await post({
      user: String(viewer.id),
      target: `environment:${scratch.envA1}`,
      was: '0',
    });
    expect(included.status()).toBe(409);
    expect(included.headers()['hx-trigger']).toBeUndefined();
    expect(await included.text()).toContain(`already included through ${scratch.projectName}`);
    const service = await post({
      user: String(viewer.id),
      target: `service:${scratch.serviceA}`,
      was: '0',
    });
    expect(service.status()).toBe(422);
    const missing = await post({ user: '987654321', target: `project:${scratch.projectId}` });
    expect(missing.status()).toBe(404);
    const gone = await post({ user: String(viewer.id), target: 'project:987654321', was: '0' });
    expect(gone.status()).toBe(409);
    expect(await gone.text()).toContain('no longer exists');
    const detail = await (await request.get(`/users/${viewer.id}`)).text();
    expect(detail.match(/<input type="checkbox"[^>]*checked/g)).toHaveLength(1);
    await expect(cell(page, viewer, `environment-${scratch.envA1}`)).toHaveAttribute(
      'data-cell',
      'inherited',
    );
  });

  test('puts the box back when the request fails', async ({ page, scratch, makeUser }) => {
    const viewer = await makeUser('viewer');
    await openGrid(page, scratch);
    const project = `project-${scratch.projectId}`;
    await page.route('**/access/grid/toggle', (route) =>
      route.fulfill({ status: 500, contentType: 'text/plain', body: 'broken' }),
    );
    await box(page, viewer, project).click();
    await expect(page.locator('[data-fragment="toast"][data-toast-source="request"]')).toBeVisible();
    await expect(box(page, viewer, project)).not.toBeChecked();
    await expect(box(page, viewer, project)).toBeEnabled();
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'none');
    await page.locator('[data-toast-source="request"] [data-js="dismiss"]').click();
    await expect(page.locator('[data-toast-source="request"]')).toHaveCount(0);
    await page.unroute('**/access/grid/toggle');
    await box(page, viewer, project).click();
    await expect(cell(page, viewer, project)).toHaveAttribute('data-cell', 'direct');
  });

  test('keeps the page within the viewport and the controls touchable', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    await openGrid(page, scratch);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await page.goto('/access/grid');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await openGrid(page, scratch);
    const tableBox = await page.locator('#grid-table .overflow-x-auto').boundingBox();
    const viewport = page.viewportSize();
    expect((tableBox?.width ?? 0) <= (viewport?.width ?? 0)).toBe(true);
    if (!isWide(page)) {
      const toggle = rowOf(page, `project-${scratch.projectId}`).locator('[data-js="tree-toggle"]');
      expect((await toggle.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
      const label = cell(page, viewer, `project-${scratch.projectId}`).locator('label');
      expect((await label.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect((await page.locator('#g-g').boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(
        TOUCH_TARGET,
      );
    }
  });

  test('is reached from the users list and the user detail', async ({ page, makeUser }) => {
    const viewer = await makeUser('viewer');
    await page.goto('/users');
    const header = page.locator('[data-fragment="page-header"]');
    await expect(header.getByRole('link', { name: 'Access grid' })).toHaveAttribute(
      'href',
      '/access/grid',
    );
    const tabs = page.locator('nav[aria-label="Sections"]');
    await expect(tabs.locator('a[aria-current="page"]')).toHaveText('Users');
    await expect(tabs.locator('a[href="/access/grid"]')).toContainText('Superadmin');
    await tabs.locator('a[href="/access/grid"]').click();
    await expect(page).toHaveURL(/\/access\/grid$/);
    await expect(page.locator('#access-grid')).toBeVisible();
    await tabs.locator('a[href="/users"]').click();
    await expect(page).toHaveURL(/\/users$/);
    await header.getByRole('link', { name: 'Access grid' }).click();
    await expect(page).toHaveURL(/\/access\/grid$/);
    await page.goto(`/users/${viewer.id}`);
    const panel = page.locator('#user-grants');
    await expect(panel.getByRole('link', { name: 'Open access grid' })).toHaveAttribute(
      'href',
      '/access/grid',
    );
    await panel.getByRole('link', { name: 'Open access grid' }).click();
    await expect(page).toHaveURL(/\/access\/grid$/);
    await expect(page.locator(`#grid-user-${viewer.id}`)).toBeVisible();
  });
});

const REFUSED_ROLES = [
  'admin',
  'viewer',
  'deploymentAdmin',
  'envScopedAdmin',
  'envScopedViewer',
  'ungrantedAdmin',
] as const;

for (const role of REFUSED_ROLES) {
  test.describe(`${role}`, () => {
    test.use({ storageState: stateFile(role) });

    test('is refused the access grid and every toggle', async ({ page }) => {
      await page.goto('/projects');
      await expect(page.locator('nav[aria-label="Primary"] a[href="/users"]')).toHaveCount(0);
      await expect(page.locator('main a[href="/access/grid"]')).toHaveCount(0);
      for (const url of ['/access/grid', '/access/grid/table', '/access/grid?group=viewer']) {
        const response = await page.request.get(url);
        expect(response.status(), url).toBe(403);
      }
      const forbidden = await page.goto('/access/grid');
      expect(forbidden?.status()).toBe(403);
      await expect(page.locator('main')).not.toContainText('e2e-u-');
      await expect(page.locator('#access-grid')).toHaveCount(0);
      const headers = await csrfHeaders(page.request);
      const response = await page.request.post('/access/grid/toggle', {
        form: { user: '1', target: 'project:1', was: '0' },
        headers,
      });
      expect(response.status()).toBe(403);
    });
  });
}
