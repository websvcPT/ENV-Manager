import type { APIRequestContext, Browser, Locator, Page } from '@playwright/test';

import { SESSION_USERS, stateFile, SUPERADMIN_STATE } from '../global-setup';
import { csrfHeaders, listProjects } from '../projects-api';
import { disableUser, listUsers, type CreatedUser } from '../users-api';
import {
  expect,
  horizontalOverflow,
  isWide,
  signedInAs,
  test,
  TOUCH_TARGET,
  type Scratch,
} from './scratch-access';

type Kind = 'project' | 'deployment' | 'service' | 'environment';

const PLURALS: Record<Kind, string> = {
  project: 'projects',
  deployment: 'deployments',
  service: 'services',
  environment: 'environments',
};

function tabPath(kind: Kind, id: number): string {
  return `/${PLURALS[kind]}/${id}/access`;
}

function list(page: Page): Locator {
  return page.locator('#access-list');
}

function row(page: Page, user: CreatedUser): Locator {
  return page.locator(`#access-row-${user.id}`);
}

function sections(page: Page): Locator {
  return page.locator('nav[aria-label="Sections"] a');
}

function addDialog(page: Page): Locator {
  return page.locator('#grant-dialog');
}

function removeDialog(page: Page): Locator {
  return page.locator('#access-remove');
}

async function openTab(page: Page, kind: Kind, id: number): Promise<void> {
  await page.goto(tabPath(kind, id));
  await expect(list(page)).toBeVisible();
}

/** Count the grants-changed events the page receives from now on. */
async function recordEvents(page: Page): Promise<void> {
  await page.evaluate(() => {
    const events: string[] = [];
    (window as unknown as { grantEvents: string[] }).grantEvents = events;
    document.addEventListener('grants-changed', () => events.push('grants-changed'));
  });
}

async function events(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { grantEvents: string[] }).grantEvents);
}

async function openAddDialog(page: Page): Promise<void> {
  await list(page).getByRole('button', { name: 'Add access' }).click();
  await expect(addDialog(page)).toBeVisible();
  await expect(addDialog(page).locator('select')).toBeVisible();
}

async function submitAdd(page: Page, user: CreatedUser): Promise<void> {
  await addDialog(page).locator('select').selectOption(String(user.id));
  await addDialog(page).getByRole('button', { name: 'Add access' }).click();
}

async function addThrough(page: Page, user: CreatedUser): Promise<void> {
  await openAddDialog(page);
  await submitAdd(page, user);
  await expect(addDialog(page)).not.toBeVisible();
}

async function openRemoveDialog(page: Page, user: CreatedUser): Promise<void> {
  await row(page, user).getByRole('button', { name: `Remove access for ${user.name}` }).click();
  await expect(removeDialog(page)).toBeVisible();
  await expect(removeDialog(page).getByRole('heading')).toContainText(
    `Remove access for ${user.name}`,
  );
}

async function removeThrough(page: Page, user: CreatedUser): Promise<void> {
  await openRemoveDialog(page, user);
  await removeDialog(page).getByRole('button', { name: 'Remove access' }).click();
  await expect(removeDialog(page)).not.toBeVisible();
}

/** Set a user's grants as the user page saves them, from the grants the caller expects. */
async function saveGrants(
  request: APIRequestContext,
  user: CreatedUser,
  grants: string[],
  base: string[],
): Promise<void> {
  const body = new URLSearchParams({ base: base.join(',') });
  for (const grant of grants) {
    body.append('grant', grant);
  }
  const response = await request.post(`/users/${user.id}/grants`, {
    data: body.toString(),
    headers: {
      ...(await csrfHeaders(request)),
      'Content-Type': 'application/x-www-form-urlencoded',
    },
  });
  expect(response.ok()).toBe(true);
}

async function postForm(
  request: APIRequestContext,
  url: string,
  form: Record<string, string>,
): Promise<{ status: number; text: string; trigger: string | undefined }> {
  const response = await request.post(url, { form, headers: await csrfHeaders(request) });
  return {
    status: response.status(),
    text: await response.text(),
    trigger: response.headers()['hx-trigger'],
  };
}

async function visibleProjects(person: Page): Promise<string> {
  await person.goto('/projects');
  return person.locator('#projects-table').innerText();
}

test.describe('superadmin: project Access tab', () => {
  test('lists direct, inherited and partial access and never a superadmin', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const direct = await makeUser('viewer', [`project:${scratch.projectId}`]);
    const viaDeployment = await makeUser('admin', [`deployment:${scratch.depA}`]);
    const viaFiles = await makeUser('viewer', [
      `environment:${scratch.envA1}`,
      `environment:${scratch.envA2}`,
    ]);
    const nothing = await makeUser('viewer');
    await openTab(page, 'project', scratch.projectId);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText(scratch.projectName);
    await expect(sections(page)).toHaveText([
      'Deployments',
      'Members and accessSuperadmin',
      'Activity',
      'SettingsSuperadmin',
    ]);
    await expect(sections(page).filter({ hasText: 'Members and access' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.locator('nav[aria-label="Breadcrumb"] [aria-current="page"]')).toHaveText(
      scratch.projectName,
    );
    await expect(list(page).locator('th[scope="col"]')).toHaveText([
      'User',
      'Group',
      'Access',
      'Actions',
    ]);
    await expect(list(page)).toContainText(`Users with access to project ${scratch.projectName}`);
    await expect(list(page)).toContainText('A project grant includes the deployments');
    await expect(list(page)).toContainText('Superadmins reach everything, so they are not listed.');
    await expect(list(page).getByRole('rowheader', { name: 'root', exact: true })).toHaveCount(0);

    await expect(row(page, direct)).toContainText('Direct');
    await expect(row(page, direct)).toContainText('Includes future items');
    await expect(row(page, direct)).toContainText('Viewer');
    await expect(
      row(page, direct).getByRole('button', { name: `Remove access for ${direct.name}` }),
    ).toBeVisible();
    await expect(row(page, viaDeployment)).toContainText('Inherited');
    await expect(row(page, viaDeployment)).toContainText('from dep-a (Deployment)');
    await expect(row(page, viaDeployment).getByRole('link', { name: /Edit at source/ })).toHaveAttribute(
      'href',
      `/deployments/${scratch.depA}/access`,
    );
    await expect(row(page, viaDeployment).getByRole('button')).toHaveCount(0);
    await expect(row(page, viaFiles)).toContainText(
      'from .env.cache (Environment), .env.cache2 (Environment)',
    );
    await expect(row(page, viaFiles).getByRole('link', { name: /Edit grants/ })).toHaveAttribute(
      'href',
      `/users/${viaFiles.id}`,
    );
    await expect(row(page, nothing)).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await row(page, viaDeployment).getByRole('link', { name: /Edit at source/ }).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${scratch.depA}/access$`));
    await expect(row(page, viaDeployment)).toContainText('Direct');
  });

  test('shows an empty state while nobody has access', async ({ page, scratch }) => {
    await openTab(page, 'project', scratch.projectId);
    await expect(list(page).locator('[data-fragment="empty-state"]')).toContainText(
      'No users have access yet',
    );
    await expect(list(page).locator('table')).toHaveCount(0);
    await expect(list(page).getByRole('button', { name: 'Add access' })).toBeVisible();
  });

  test('adds a grant through the dialog, tells the panels and applies at once', async ({
    page,
    browser,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer');
    await openTab(page, 'project', scratch.projectId);
    await recordEvents(page);
    await openAddDialog(page);
    const dialog = addDialog(page);
    await expect(dialog.getByRole('heading', { name: 'Add access' })).toBeVisible();
    await expect(dialog).toContainText(`project ${scratch.projectName}`);
    await expect(dialog.locator(`select option[value="${viewer.id}"]`)).toHaveText(
      `${viewer.name} (Viewer)`,
    );
    await expect(dialog.locator('select option', { hasText: '(Superadmin)' })).toHaveCount(0);
    await expect(dialog.getByRole('note')).toContainText(
      `Includes future items: deployments, services and environments added under project ${scratch.projectName} later are visible to this user automatically.`,
    );
    const person = await signedInAs(browser, viewer);
    try {
      expect(await visibleProjects(person)).not.toContain(scratch.projectName);
      await submitAdd(page, viewer);
      await expect(dialog).not.toBeVisible();
      await expect(row(page, viewer)).toContainText('Direct');
      await expect(row(page, viewer)).toContainText('Includes future items');
      expect(await events(page)).toEqual(['grants-changed']);
      expect(await visibleProjects(person)).toContain(scratch.projectName);
    } finally {
      await person.context().close();
    }
    await openAddDialog(page);
    await expect(addDialog(page).locator(`select option[value="${viewer.id}"]`)).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(addDialog(page)).not.toBeVisible();
  });

  test('asks before removing a grant and takes access away at once', async ({
    page,
    browser,
    scratch,
    makeUser,
  }) => {
    const viewer = await makeUser('viewer', [`project:${scratch.projectId}`]);
    await openTab(page, 'project', scratch.projectId);
    await recordEvents(page);
    const person = await signedInAs(browser, viewer);
    try {
      expect(await visibleProjects(person)).toContain(scratch.projectName);
      await openRemoveDialog(page, viewer);
      const confirm = removeDialog(page);
      await expect(confirm).toContainText(
        `${viewer.name} loses the grant on project ${scratch.projectName} and on the items added under it later.`,
      );
      await expect(confirm).toContainText('including to their API tokens');
      await confirm.getByRole('button', { name: 'Cancel' }).click();
      await expect(confirm).not.toBeVisible();
      await expect(row(page, viewer)).toBeVisible();
      expect(await events(page)).toEqual([]);

      await removeThrough(page, viewer);
      await expect(row(page, viewer)).toHaveCount(0);
      await expect(list(page).locator('[data-fragment="empty-state"]')).toBeVisible();
      expect(await events(page)).toEqual(['grants-changed']);
      expect(await visibleProjects(person)).not.toContain(scratch.projectName);
    } finally {
      await person.context().close();
    }
  });

  test('keeps the old members address working', async ({ page, scratch }) => {
    await page.goto(`/projects/${scratch.projectId}/members`);
    await expect(page).toHaveURL(new RegExp(`/projects/${scratch.projectId}/access$`));
    await expect(list(page)).toBeVisible();
  });
});

test.describe('superadmin: deployment Access tab', () => {
  test('has the sections of the page and lists, adds and removes direct grants', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const inherited = await makeUser('viewer', [`project:${scratch.projectId}`]);
    const viaFile = await makeUser('admin', [`environment:${scratch.envA1}`]);
    const added = await makeUser('admin');
    await openTab(page, 'deployment', scratch.depA);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('dep-a');
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
      `Project ${scratch.projectName}`,
    );
    await expect(page.locator('[data-fragment="page-header"] a, [data-fragment="page-header"] button')).toHaveCount(0);
    await expect(sections(page)).toHaveText(['Services', 'Deploy history', 'AccessSuperadmin']);
    await expect(sections(page).nth(0)).toHaveAttribute('href', `/deployments/${scratch.depA}`);
    await expect(sections(page).nth(1)).toHaveAttribute('href', `/deployments/${scratch.depA}#hist`);
    await expect(sections(page).nth(2)).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('nav[aria-label="Breadcrumb"] [aria-current="page"]')).toHaveText('dep-a');
    await expect(list(page)).toContainText('A deployment grant includes the services and environments');
    await expect(row(page, inherited)).toContainText(`from ${scratch.projectName} (Project)`);
    await expect(row(page, inherited).getByRole('link', { name: /Edit at source/ })).toHaveAttribute(
      'href',
      `/projects/${scratch.projectId}/access`,
    );
    await expect(row(page, viaFile)).toContainText('from .env.cache (Environment)');
    await expect(row(page, viaFile).getByRole('link', { name: /Edit at source/ })).toHaveAttribute(
      'href',
      `/environments/${scratch.envA1}/access`,
    );

    await openAddDialog(page);
    await expect(addDialog(page).getByRole('note')).toContainText(
      'Includes future items: services and environments added under deployment dep-a later are visible to this user automatically.',
    );
    await expect(addDialog(page).locator(`select option[value="${inherited.id}"]`)).toHaveCount(0);
    await expect(addDialog(page).locator(`select option[value="${viaFile.id}"]`)).toHaveCount(1);
    await page.keyboard.press('Escape');
    await addThrough(page, added);
    await expect(row(page, added)).toContainText('Direct');
    await expect(row(page, added)).toContainText('Includes future items');
    await removeThrough(page, added);
    await expect(row(page, added)).toHaveCount(0);
    await expect(row(page, inherited)).toBeVisible();

    await sections(page).nth(0).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${scratch.depA}$`));
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('dep-a');
    await expect(sections(page)).toHaveText(['Services', 'Deploy history', 'AccessSuperadmin']);
    await sections(page).nth(2).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${scratch.depA}/access$`));
  });

  test('a user granted the project is not offered the deployment', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const covered = await makeUser('viewer', [`project:${scratch.projectId}`]);
    await openTab(page, 'deployment', scratch.depB);
    await openAddDialog(page);
    await expect(addDialog(page).locator(`select option[value="${covered.id}"]`)).toHaveCount(0);
  });
});

test.describe('superadmin: environment Access tab', () => {
  test('is reached from the editor and lists, adds and removes direct grants', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const inherited = await makeUser('viewer', [`deployment:${scratch.depA}`]);
    const added = await makeUser('viewer');
    await page.goto(`/environments/${scratch.envA1}`);
    await expect(sections(page)).toHaveText(['Edit', 'History', 'Compare to dist', 'AccessSuperadmin']);
    await sections(page).filter({ hasText: 'Access' }).click();
    await expect(page).toHaveURL(new RegExp(`/environments/${scratch.envA1}/access$`));
    await expect(sections(page).filter({ hasText: 'Access' })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('.env.cache');
    await expect(list(page)).toContainText('Users with access to environment .env.cache');
    await expect(list(page)).not.toContainText('A project grant includes');
    await expect(row(page, inherited)).toContainText('from dep-a (Deployment)');

    await openAddDialog(page);
    await expect(addDialog(page).getByRole('note')).toContainText(
      'Grants only the named environments. A newly added environment stays invisible to this user until a grant is added for it.',
    );
    await page.keyboard.press('Escape');
    await addThrough(page, added);
    await expect(row(page, added)).toContainText('Direct');
    await expect(row(page, added)).not.toContainText('future');
    await removeThrough(page, added);
    await expect(row(page, added)).toHaveCount(0);

    await page.goto(`/environments/${scratch.envA1}/history`);
    await expect(sections(page).filter({ hasText: 'Access' })).toHaveAttribute(
      'href',
      `/environments/${scratch.envA1}/access`,
    );
  });
});

test.describe('superadmin: service Access tab', () => {
  test('shows the result, links to the grant levels and has no grant controls', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const viaProject = await makeUser('viewer', [`project:${scratch.projectId}`]);
    const viaDeployment = await makeUser('admin', [`deployment:${scratch.depA}`]);
    const viaFile = await makeUser('viewer', [`environment:${scratch.envA1}`]);
    await openTab(page, 'service', scratch.serviceA);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('cache');
    await expect(page.locator('[data-fragment="page-header"]')).toContainText('Service');
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
      `Deployment dep-a in ${scratch.projectName}`,
    );
    await expect(sections(page)).toHaveText(['Environments', 'Deploy history', 'AccessSuperadmin']);
    await expect(sections(page).nth(0)).toHaveAttribute('href', `/services/${scratch.serviceA}`);
    await expect(list(page)).toContainText('Services are not a grant level');
    await expect(list(page).getByRole('button')).toHaveCount(0);
    await expect(page.locator('#grant-dialog, #access-remove')).toHaveCount(0);
    await expect(row(page, viaProject)).toContainText(`from ${scratch.projectName} (Project)`);
    await expect(row(page, viaDeployment)).toContainText('from dep-a (Deployment)');
    await expect(row(page, viaFile)).toContainText('from .env.cache (Environment), sees 1 of 2');
    await expect(row(page, viaProject)).not.toContainText('sees');
    await expect(row(page, viaFile).getByRole('link', { name: /Edit at source/ })).toHaveAttribute(
      'href',
      `/environments/${scratch.envA1}/access`,
    );
    await expect(
      list(page).getByRole('link', { name: `deployment dep-a`, exact: true }),
    ).toHaveAttribute('href', `/deployments/${scratch.depA}/access`);
    await expect(
      list(page).getByRole('link', { name: `project ${scratch.projectName}`, exact: true }),
    ).toHaveAttribute('href', `/projects/${scratch.projectId}/access`);

    await list(page).getByRole('link', { name: 'deployment dep-a', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${scratch.depA}/access$`));
    await expect(row(page, viaDeployment)).toContainText('Direct');
  });

  test('says when no one reaches the service', async ({ page, scratch }) => {
    await openTab(page, 'service', scratch.serviceB);
    await expect(list(page).locator('[data-fragment="empty-state"]')).toContainText(
      'No users reach this service',
    );
  });

  test('has no routes to change a grant', async ({ page, scratch, makeUser, request }) => {
    const viewer = await makeUser('viewer');
    const base = tabPath('service', scratch.serviceA);
    expect((await page.request.get(`${base}/add`)).status()).toBe(404);
    expect((await page.request.get(`${base}/remove/${viewer.id}`)).status()).toBe(404);
    expect((await postForm(request, `${base}/grants`, { user: String(viewer.id) })).status).toBe(404);
    expect((await postForm(request, `${base}/remove/${viewer.id}`, {})).status).toBe(404);
  });
});

test.describe('superadmin: refusals', () => {
  test('refuses an add from an out of date dialog and redraws the list', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer');
    await openTab(page, 'project', scratch.projectId);
    await recordEvents(page);
    await openAddDialog(page);
    await addDialog(page).locator('select').selectOption(String(viewer.id));
    await saveGrants(request, viewer, [`project:${scratch.projectId}`], []);
    await expect(row(page, viewer)).toHaveCount(0);
    await addDialog(page).getByRole('button', { name: 'Add access' }).click();
    await expect(addDialog(page).locator('[role="alert"]')).toContainText(
      'This access changed since the page was loaded. The list shows the current state.',
    );
    await expect(addDialog(page)).toBeVisible();
    await expect(row(page, viewer)).toContainText('Direct');
    expect(await events(page)).toEqual([]);
    await addDialog(page).locator('[data-js="dialog-close"]').first().click();
    await expect(addDialog(page)).not.toBeVisible();
  });

  test('refuses an add that an item above already includes', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer');
    await openTab(page, 'environment', scratch.envA1);
    await openAddDialog(page);
    await addDialog(page).locator('select').selectOption(String(viewer.id));
    await saveGrants(request, viewer, [`project:${scratch.projectId}`], []);
    await addDialog(page).getByRole('button', { name: 'Add access' }).click();
    await expect(addDialog(page).locator('[role="alert"]')).toContainText(
      `Access to this item is already included through ${scratch.projectName}.`,
    );
    await expect(row(page, viewer)).toContainText(`from ${scratch.projectName} (Project)`);
    await expect(row(page, viewer).getByRole('button')).toHaveCount(0);
  });

  test('refuses a remove from an out of date page and redraws the list', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer', [`project:${scratch.projectId}`]);
    await openTab(page, 'project', scratch.projectId);
    await recordEvents(page);
    await saveGrants(request, viewer, [], [`project:${scratch.projectId}`]);
    await expect(row(page, viewer)).toBeVisible();
    await row(page, viewer).getByRole('button', { name: `Remove access for ${viewer.name}` }).click();
    await expect(removeDialog(page)).toBeVisible();
    await expect(removeDialog(page).locator('[role="alert"]')).toContainText(
      'This access changed since the page was loaded. The list shows the current state.',
    );
    await expect(removeDialog(page).getByRole('button', { name: 'Remove access' })).toHaveCount(0);
    await expect(row(page, viewer)).toHaveCount(0);
    expect(await events(page)).toEqual([]);
    await removeDialog(page).getByText('Close', { exact: true }).click();
    await expect(removeDialog(page)).not.toBeVisible();
  });

  test('refuses a confirmed remove whose grant was removed meanwhile', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer', [`project:${scratch.projectId}`]);
    await openTab(page, 'project', scratch.projectId);
    await openRemoveDialog(page, viewer);
    await saveGrants(request, viewer, [], [`project:${scratch.projectId}`]);
    await removeDialog(page).getByRole('button', { name: 'Remove access' }).click();
    await expect(removeDialog(page).locator('[role="alert"]')).toContainText(
      'changed since the page was loaded',
    );
    await expect(removeDialog(page)).toBeVisible();
    await expect(row(page, viewer)).toHaveCount(0);
  });

  test('refuses what the page never offers and writes nothing', async ({
    page,
    scratch,
    makeUser,
    request,
  }) => {
    const viewer = await makeUser('viewer');
    const disabled = await makeUser('viewer');
    await disableUser(request, disabled.id);
    const root = (await listUsers(request)).find(([, name]) => name === 'root');
    expect(root).toBeTruthy();
    const base = tabPath('project', scratch.projectId);
    const add = async (user: string) => postForm(request, `${base}/grants`, { user });

    const none = await add('');
    expect(none.status).toBe(422);
    expect(none.text).toContain('Choose a user.');
    expect((await add('abc')).status).toBe(422);
    const off = await add(String(disabled.id));
    expect(off.status).toBe(409);
    expect(off.text).toContain('This account is disabled, so it is not listed here.');
    const superadmin = await add(String(root?.[0]));
    expect(superadmin.status).toBe(409);
    expect(superadmin.text).toContain('Superadmins reach every project');
    const missing = await add('987654321');
    expect(missing.status).toBe(409);
    expect(missing.text).toContain('This user no longer exists.');
    for (const refused of [off, superadmin, missing]) {
      expect(refused.trigger).toBeUndefined();
    }
    const removed = await postForm(request, `${base}/remove/${viewer.id}`, {});
    expect(removed.status).toBe(409);
    expect(removed.trigger).toBeUndefined();
    expect((await page.request.get(`${base}/remove/987654321`)).status()).toBe(404);
    expect((await page.request.get(tabPath('project', 987654321))).status()).toBe(404);
    for (const kind of Object.keys(PLURALS) as Kind[]) {
      expect((await page.request.get(`${tabPath(kind, 987654321)}/list`)).status(), kind).toBe(404);
    }
    for (const user of [viewer, disabled]) {
      const detail = await (await request.get(`/users/${user.id}`)).text();
      expect(detail.match(/<input type="checkbox"[^>]*checked/g) ?? []).toHaveLength(0);
    }
    await openTab(page, 'project', scratch.projectId);
    await expect(list(page).locator('[data-fragment="empty-state"]')).toBeVisible();
  });
});

test.describe('superadmin: small screens', () => {
  test('keeps every tab within the viewport and the controls touchable', async ({
    page,
    scratch,
    makeUser,
  }) => {
    const direct = await makeUser('viewer', [`project:${scratch.projectId}`]);
    const inherited = await makeUser('admin', [`deployment:${scratch.depA}`]);
    const tabs: Array<[Kind, number]> = [
      ['project', scratch.projectId],
      ['deployment', scratch.depA],
      ['service', scratch.serviceA],
      ['environment', scratch.envA1],
    ];
    for (const [kind, id] of tabs) {
      await openTab(page, kind, id);
      expect(await horizontalOverflow(page), kind).toBeLessThanOrEqual(0);
      const panel = await list(page).boundingBox();
      expect((panel?.width ?? 0) <= (page.viewportSize()?.width ?? 0), kind).toBe(true);
    }
    if (isWide(page)) {
      return;
    }
    await openTab(page, 'project', scratch.projectId);
    const add = await list(page).getByRole('button', { name: 'Add access' }).boundingBox();
    expect(add?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    const remove = await row(page, direct).getByRole('button', { name: /Remove/ }).boundingBox();
    expect(remove?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    const edit = await row(page, inherited).getByRole('link', { name: /Edit at source/ }).boundingBox();
    expect(edit?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    await openAddDialog(page);
    const dialog = await addDialog(page).boundingBox();
    expect((dialog?.width ?? 0) <= (page.viewportSize()?.width ?? 0)).toBe(true);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

interface Seeded {
  fancy: number;
  prod: number;
  staging: number;
  mysql: number;
  db: number;
  secrets: number;
}

function idOf(html: string, prefix: string, name: string): number {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const found = new RegExp(`<a href="${prefix}(\\d+)"[^>]*>${escaped}</a>`).exec(html);
  if (!found) {
    throw new Error(`seeded ${name} is missing`);
  }
  return Number(found[1]);
}

/** Ids of the seeded items by name, read as the superadmin so any role's spec can use them. */
async function seededIds(browser: Browser): Promise<Seeded> {
  const context = await browser.newContext({ storageState: SUPERADMIN_STATE });
  try {
    const get = async (url: string) => (await context.request.get(url)).text();
    const found = (await listProjects(context.request, 'Fancy Proj. Name')).find(
      ([, name]) => name === 'Fancy Proj. Name',
    );
    if (!found) {
      throw new Error('seeded project Fancy Proj. Name is missing');
    }
    const fancy = found[0];
    const project = await get(`/projects/${fancy}`);
    const prod = idOf(project, '/deployments/', 'prod');
    const staging = idOf(project, '/deployments/', 'staging');
    const mysql = idOf(await get(`/deployments/${prod}`), '/services/', 'mysql');
    const service = await get(`/services/${mysql}`);
    const db = idOf(service, '/environments/', '.env.db');
    const secrets = idOf(service, '/environments/', '.env.db.secrets');
    return { fancy, prod, staging, mysql, db, secrets };
  } finally {
    await context.close();
  }
}

/** The status each seeded role gets from an item's Access routes: 403 in scope, 404 outside. */
const ROLES: Array<[keyof typeof SESSION_USERS, Partial<Record<keyof Seeded | 'all', number>>]> = [
  ['admin', {}],
  ['viewer', {}],
  ['deploymentAdmin', { staging: 404 }],
  ['envScopedAdmin', { staging: 404, secrets: 404 }],
  ['envScopedViewer', { staging: 404, secrets: 404 }],
  ['ungrantedAdmin', { all: 404 }],
];

for (const [role, outside] of ROLES) {
  test.describe(`${role}`, () => {
    test.use({ storageState: stateFile(role) });

    test('has no Access tab and is refused every Access route', async ({ page, browser }) => {
      const seeded = await seededIds(browser);
      const items: Array<[Kind, keyof Seeded]> = [
        ['project', 'fancy'],
        ['deployment', 'prod'],
        ['deployment', 'staging'],
        ['service', 'mysql'],
        ['environment', 'db'],
        ['environment', 'secrets'],
      ];
      const headers = await csrfHeaders(page.request);
      for (const [kind, name] of items) {
        const want = outside.all ?? outside[name] ?? 403;
        const base = tabPath(kind, seeded[name]);
        const requests: Array<['GET' | 'POST', string]> = [
          ['GET', base],
          ['GET', `${base}/list`],
        ];
        if (kind !== 'service') {
          requests.push(
            ['GET', `${base}/add`],
            ['POST', `${base}/grants`],
            ['GET', `${base}/remove/1`],
            ['POST', `${base}/remove/1`],
          );
        }
        for (const [method, url] of requests) {
          const response =
            method === 'GET'
              ? await page.request.get(url, { headers: { 'HX-Request': 'true' } })
              : await page.request.post(url, { form: { user: '1' }, headers });
          expect(response.status(), `${role} ${method} ${url}`).toBe(want);
          expect(await response.text()).not.toContain('access-row');
        }
      }
      if (outside.all) {
        return;
      }
      await page.goto(`/projects/${seeded.fancy}`);
      await expect(sections(page).filter({ hasText: 'Access' })).toHaveCount(0);
      await page.goto(`/environments/${seeded.db}`);
      await expect(sections(page).filter({ hasText: 'Access' })).toHaveCount(0);
      await page.goto(`/environments/${seeded.db}/history`);
      await expect(sections(page).filter({ hasText: 'Access' })).toHaveCount(0);
      await expect(page.locator('main a[href$="/access"]')).toHaveCount(0);
      await page.goto(`/deployments/${seeded.prod}`);
      await expect(sections(page).filter({ hasText: 'Access' })).toHaveCount(0);
      await page.goto(`/services/${seeded.mysql}`);
      await expect(sections(page).filter({ hasText: 'Access' })).toHaveCount(0);
    });
  });
}
