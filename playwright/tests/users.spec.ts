import { expect, test as base, type Browser, type Locator, type Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { stateFile } from '../global-setup';
import { csrfHeaders } from '../projects-api';
import { createService } from '../services-api';
import {
  createUser,
  disableUser,
  listUsers,
  uniqueUserName,
  type CreatedUser,
} from '../users-api';
import { createProject, deleteProjectIfPresent } from '../projects-api';

const WIDE_BREAKPOINT = 768;
const SAMPLE_A = '/srv/deployments/sample-a';

interface Scratch {
  projectId: number;
  projectName: string;
  deploymentId: number;
  environmentId: number;
}

/**
 * The base test plus a scratch project with one deployment, service and environment (the tree to
 * grant from) and a `makeUser` that creates users through the form post and disables them again
 * after the test, because users cannot be deleted.
 */
const test = base.extend<{
  scratch: Scratch;
  makeUser: (group?: 'admin' | 'viewer', grants?: string[]) => Promise<CreatedUser>;
}>({
  scratch: async ({ request }, use, testInfo) => {
    const suffix = Math.random().toString(36).slice(2, 7);
    const projectName = `e2e-detail-${Date.now()}-${suffix}-${testInfo.project.name}`;
    const projectId = await createProject(request, projectName);
    try {
      const deploymentId = await createDeployment(request, projectId, 'scratch-dep', SAMPLE_A);
      const serviceId = await createService(request, deploymentId, 'cache');
      const environmentId = await createEnvironment(request, serviceId, '.env.cache', 'A=1\n');
      await use({ projectId, projectName, deploymentId, environmentId });
    } finally {
      await deleteProjectIfPresent(request, projectId);
    }
  },
  makeUser: async ({ request }, use, testInfo) => {
    const created: CreatedUser[] = [];
    await use(async (group = 'viewer', grants = []) => {
      const user = await createUser(
        request,
        uniqueUserName(testInfo.project.name),
        group,
        grants,
      );
      created.push(user);
      return user;
    });
    for (const user of created) {
      await disableUser(request, user.id);
    }
  },
});

/**
 * Save the grant tree and wait for the server's answer, so a note left by an earlier save never
 * stands in for the answer of this one.
 */
async function saveGrants(page: Page, grants: Locator): Promise<void> {
  const answered = page.waitForResponse(
    (response) => response.url().endsWith('/grants') && response.request().method() === 'POST',
  );
  await grants.getByRole('button', { name: 'Save grants' }).click();
  await answered;
  await expect(grants.getByText('Grants saved. They apply now.')).toBeVisible();
}

function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

function row(page: Page, name: string) {
  return page.locator('[data-fragment="user-row"]', {
    has: page.locator('th[scope="row"] a', { hasText: new RegExp(`^${name}$`) }),
  });
}

function rowNames(page: Page) {
  return page.locator('#users-table [data-fragment="user-row"] th[scope="row"] a');
}

async function openCreate(page: Page) {
  await page.locator('[data-js="dialog-open"][data-target="user-create"]').first().click();
  const dialog = page.locator('#user-create[open]');
  await expect(dialog.locator('#un')).toBeVisible();
  return dialog;
}

/** Sign in through the login form in a browser of its own and return the page. */
async function signedInAs(browser: Browser, user: CreatedUser): Promise<Page> {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const page = await context.newPage();
  await page.goto('/login');
  await page.locator('#user').fill(user.name);
  await page.locator('#pw').fill(user.password);
  await page.locator('#login-card button[type="submit"]').click();
  await expect(page).toHaveURL(/\/projects$/);
  return page;
}

test.describe('superadmin: the users list', () => {
  test('lists every account with its group, status, last login and reach', async ({ page }) => {
    await page.goto('/users');
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('Users');
    await expect(page.getByText('Accounts are created by a superadmin.')).toBeVisible();
    await expect(page.locator('#users-table th[scope="col"]')).toHaveText([
      'Name',
      'Email',
      'Group',
      'Status',
      'Last login',
      'Projects',
      'Actions',
    ]);
    await expect(page.locator('nav[aria-label="Primary"] a[aria-current="page"]').first()).toHaveText(
      /Users/,
    );
    await expect(page.locator('nav[aria-label="Breadcrumb"] [aria-current="page"]')).toHaveText(
      'Users',
    );
    const root = row(page, 'root');
    await expect(root.locator('td').nth(1)).toHaveText('Superadmin');
    await expect(root.locator('td').nth(2)).toHaveText('Active');
    await expect(root.locator('td').nth(4)).toHaveText('All');
    await expect(root.getByRole('link', { name: 'Edit' })).toHaveAttribute('href', /^\/users\/\d+$/);
    const admin = row(page, 'admin');
    await expect(admin.locator('td').nth(0)).toHaveText('admin@example.test');
    await expect(admin.locator('td').nth(1)).toHaveText('Admin');
    await expect(admin.locator('td').nth(4)).toHaveText('2');
    await expect(row(page, 'viewer').locator('td').nth(1)).toHaveText('Viewer');
    await expect(row(page, 'ungranted_admin').locator('td').nth(4)).toHaveText('0');
    await expect(
      page.locator('[data-js="dialog-open"][data-target="user-create"]').first(),
    ).toBeVisible();
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('opens the create dialog with the grant tree and closes it with Cancel', async ({
    page,
    scratch,
  }) => {
    await page.goto('/users');
    const dialog = await openCreate(page);
    await expect(dialog.getByRole('heading', { name: 'Create user' })).toBeVisible();
    await expect(dialog.getByText('Superadmin only')).toBeVisible();
    await expect(dialog.locator('#ug')).toHaveValue('viewer');
    await expect(dialog.locator('#ug option')).toHaveText(['Viewer', 'Admin', 'Superadmin']);
    const tree = dialog.locator('[data-fragment="grant-tree"]');
    await expect(tree.getByText(`Project ${scratch.projectName}`)).toBeVisible();
    await expect(tree.getByText('Includes future deployments, services and environments.').first()).toBeVisible();
    await expect(tree.getByText('Includes future services and environments.').first()).toBeVisible();
    await expect(tree.getByText('(groups environments, not a grant level)').first()).toBeVisible();
    await expect(tree.locator('#grant-project-' + scratch.projectId)).not.toBeChecked();
    await expect(tree.locator('#grant-deployment-' + scratch.deploymentId)).not.toBeChecked();
    await expect(tree.locator('#grant-environment-' + scratch.environmentId)).not.toBeChecked();
    await expect(tree.locator('input[type="checkbox"]:checked')).toHaveCount(0);
    await expect(
      dialog.getByText('A 16-character password is generated and shown once after creation.'),
    ).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('#user-create[open]')).toHaveCount(0);
  });

  test('refuses bad and duplicate input next to the field and keeps the choices', async ({
    page,
    scratch,
  }) => {
    const name = uniqueUserName(test.info().project.name);
    await page.goto('/users');
    const dialog = await openCreate(page);
    await dialog.locator('#un').fill('has space');
    await dialog.locator('#ue').fill('someone@example.test');
    await dialog.locator('#ug').selectOption('admin');
    await dialog.locator('#grant-deployment-' + scratch.deploymentId).check();
    await dialog.getByRole('button', { name: 'Create user' }).click();
    await expect(dialog.locator('#un-e[role="alert"]')).toContainText(
      'Use 1 to 100 characters without spaces.',
    );
    await expect(dialog.locator('#un')).toHaveAttribute('aria-invalid', 'true');
    await expect(dialog.locator('#un')).toHaveValue('has space');
    await expect(dialog.locator('#ue')).toHaveValue('someone@example.test');
    await expect(dialog.locator('#ug')).toHaveValue('admin');
    await expect(dialog.locator('#grant-deployment-' + scratch.deploymentId)).toBeChecked();

    await dialog.locator('#un').fill('ROOT');
    await dialog.getByRole('button', { name: 'Create user' }).click();
    await expect(dialog.locator('#un-e[role="alert"]')).toContainText(
      'A user named ROOT already exists.',
    );

    await dialog.locator('#un').fill(name);
    await dialog.locator('#ue').fill('Admin@Example.TEST');
    await dialog.getByRole('button', { name: 'Create user' }).click();
    await expect(dialog.locator('#ue-e[role="alert"]')).toContainText(
      'Another user already has this email address.',
    );
    await expect(dialog.locator('#un-e')).toHaveCount(0);
    await expect(dialog.locator('#un')).toHaveValue(name);
    await expect(dialog.locator('#secret')).toHaveCount(0);
    await dialog.locator('[data-js="dialog-close"]').last().click();
    expect((await listUsers(page.request)).some(([, listed]) => listed === name)).toBe(false);
  });

  test('creates a user with grants, shows the password once and clears it on close', async ({
    page,
    browser,
    scratch,
    request,
  }) => {
    const name = uniqueUserName(test.info().project.name);
    const email = `${name}@example.test`;
    await page.goto('/users');
    const dialog = await openCreate(page);
    await dialog.locator('#un').fill(name);
    await dialog.locator('#ue').fill(email);
    await dialog.locator('#grant-project-' + scratch.projectId).check();
    await dialog.getByRole('button', { name: 'Create user' }).click();

    await expect(dialog.getByRole('heading', { name: 'User created' })).toBeVisible();
    await expect(dialog.getByText(`${name} · Viewer`)).toBeVisible();
    await expect(
      dialog.getByText('Shown once. Copy it now and pass it on securely.'),
    ).toBeVisible();
    const secret = dialog.locator('#secret');
    await expect(secret).toHaveAttribute('readonly', '');
    const password = await secret.inputValue();
    expect(password).toHaveLength(16);
    await dialog.locator('[data-js="copy"]').click();
    await expect(dialog.locator('[data-copy-label]')).toHaveText('Copied');

    await expect(row(page, name)).toBeVisible();
    await expect(row(page, name).locator('td').nth(1)).toHaveText('Viewer');
    await expect(row(page, name).locator('td').nth(2)).toHaveText('Active');
    await expect(row(page, name).locator('td').nth(3)).toHaveText('Never');
    await expect(row(page, name).locator('td').nth(4)).toHaveText('1');

    await dialog.getByRole('button', { name: 'Done' }).click();
    await expect(page.locator('#user-create[open]')).toHaveCount(0);
    expect(await page.content()).not.toContain(password);
    const again = await openCreate(page);
    await expect(again.locator('#secret')).toHaveCount(0);
    await expect(again.locator('#un')).toHaveValue('');
    expect(await page.content()).not.toContain(password);
    await again.locator('[data-js="dialog-close"]').last().click();

    const id = (await listUsers(request)).find(([, listed]) => listed === name)?.[0] ?? 0;
    expect(id).toBeGreaterThan(0);
    try {
      const person = await signedInAs(browser, { id, name, email, password });
      await expect(person.locator('#projects-table')).toContainText(scratch.projectName);
      await expect(person.locator('nav[aria-label="Primary"] a[href="/users"]')).toHaveCount(0);

      await row(page, name).getByRole('button', { name: `Disable ${name}` }).click();
      await page.locator('#user-action[open]').getByRole('button', { name: 'Disable user' }).click();
      await expect(page.locator('#user-action[open]')).toHaveCount(0);
      await expect(row(page, name).locator('td').nth(2)).toHaveText('Disabled');
      await person.reload();
      await expect(person).toHaveURL(/\/login/);
      await person.context().close();
    } finally {
      await disableUser(request, id);
    }
  });

  test('disables and enables from the list through confirm dialogs', async ({ page, makeUser }) => {
    const user = await makeUser();
    await page.goto('/users');
    const current = row(page, user.name);
    await current.getByRole('button', { name: `Disable ${user.name}` }).click();
    const dialog = page.locator('#user-action[open]');
    await expect(dialog.getByRole('heading', { name: `Disable ${user.name}` })).toBeVisible();
    await expect(dialog.getByText('active sessions end now')).toBeVisible();
    await expect(dialog.getByText('All of their API tokens are revoked for good')).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('#user-action[open]')).toHaveCount(0);
    await expect(current.locator('td').nth(2)).toHaveText('Active');

    await current.getByRole('button', { name: `Disable ${user.name}` }).click();
    await dialog.getByRole('button', { name: 'Disable user' }).click();
    await expect(page.locator('#user-action[open]')).toHaveCount(0);
    await expect(current.locator('td').nth(2)).toHaveText('Disabled');
    await expect(current.getByRole('button', { name: `Enable ${user.name}` })).toBeVisible();

    await current.getByRole('button', { name: `Enable ${user.name}` }).click();
    await expect(dialog.getByRole('heading', { name: `Enable ${user.name}` })).toBeVisible();
    await expect(dialog.getByText('Their old API tokens stay revoked')).toBeVisible();
    await dialog.getByRole('button', { name: 'Enable user' }).click();
    await expect(page.locator('#user-action[open]')).toHaveCount(0);
    await expect(current.locator('td').nth(2)).toHaveText('Active');
  });

  test('shows an unknown user as not found', async ({ page }) => {
    const response = await page.goto('/users/987654');
    expect(response?.status()).toBe(404);
    await expect(page.locator('main')).not.toContainText('987654');
  });
});

test.describe('superadmin: the user detail', () => {
  test('shows the account, its grants, tokens and activity', async ({ page }) => {
    await page.goto('/users');
    await row(page, 'admin').getByRole('link', { name: 'Edit' }).click();
    await expect(page).toHaveURL(/\/users\/\d+$/);
    const header = page.locator('[data-fragment="page-header"]');
    await expect(header.locator('h1')).toHaveText('admin');
    await expect(header).toContainText('Admin');
    await expect(header).toContainText('Active');
    await expect(header).toContainText('admin@example.test');
    await expect(page.locator('nav[aria-label="Breadcrumb"] [aria-current="page"]')).toHaveText(
      'admin',
    );
    await expect(page.locator('#g2')).toHaveValue('admin');
    await expect(
      page.locator('#user-grants [data-fragment="grant-tree"] input:checked'),
    ).toHaveCount(2);
    const tokens = page.locator('[data-fragment="user-tokens"]');
    await expect(tokens.locator('th[scope="col"]')).toHaveText([
      'Name',
      'Last used',
      'Status',
      'Actions',
    ]);
    const token = tokens.locator('[data-fragment="token-row"]', { hasText: 'billing-ci' });
    await expect(token).toContainText('Active');
    await expect(token.getByRole('button', { name: /^Revoke/ })).toHaveCount(1);
    const activity = page.locator('[data-fragment="user-activity"]');
    await expect(activity.getByRole('link', { name: 'Open audit log' })).toHaveAttribute(
      'href',
      '/audit?actor=user:admin',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('shows a sign-in in the recent activity and says so when there is none', async ({
    page,
    browser,
    makeUser,
  }) => {
    const quiet = await makeUser();
    await page.goto(`/users/${quiet.id}`);
    await expect(page.locator('[data-fragment="user-tokens"]')).toContainText('No API tokens');
    await expect(page.locator('[data-fragment="user-activity"]')).toContainText('No activity yet');
    const person = await signedInAs(browser, quiet);
    await person.context().close();
    await page.reload();
    const entry = page.locator('[data-fragment="activity-row"]').first();
    await expect(entry).toContainText('login.success');
    await expect(entry).toContainText('Success');
  });

  test('changes the group after a confirm dialog and keeps it when cancelled', async ({
    page,
    makeUser,
  }) => {
    const user = await makeUser();
    await page.goto(`/users/${user.id}`);
    await page.locator('#g2').selectOption('admin');
    await page.getByRole('button', { name: 'Save group' }).click();
    const dialog = page.locator('#user-action[open]');
    await expect(dialog.getByRole('heading', { name: `Change group of ${user.name}` })).toBeVisible();
    await expect(dialog.getByText(`Move ${user.name} from Viewer to Admin.`)).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await page.reload();
    await expect(page.locator('#g2')).toHaveValue('viewer');

    await page.locator('#g2').selectOption('admin');
    await page.getByRole('button', { name: 'Save group' }).click();
    await dialog.getByRole('button', { name: 'Change group' }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${user.id}\\?updated=group$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText(
      `${user.name} is now an Admin.`,
    );
    await expect(page.locator('#g2')).toHaveValue('admin');
    await expect(page.locator('[data-fragment="page-header"]')).toContainText('Admin');

    await page.locator('#g2').selectOption('admin');
    await page.getByRole('button', { name: 'Save group' }).click();
    await expect(dialog.getByText(`${user.name} is already in the Admin group.`)).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Change group' })).toHaveCount(0);
  });

  test('disables and enables from the header through confirm dialogs', async ({
    page,
    makeUser,
  }) => {
    const user = await makeUser();
    await page.goto(`/users/${user.id}`);
    await page.locator('[data-fragment="page-header"]').getByRole('button', { name: 'Disable' }).click();
    const dialog = page.locator('#user-action[open]');
    await dialog.getByRole('button', { name: 'Disable user' }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${user.id}\\?updated=disabled$`));
    await expect(page.locator('[data-fragment="toast"]')).toContainText('User disabled');
    await expect(page.locator('[data-fragment="page-header"]')).toContainText('Disabled');

    await page.locator('[data-fragment="page-header"]').getByRole('button', { name: 'Enable' }).click();
    await dialog.getByRole('button', { name: 'Enable user' }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${user.id}\\?updated=enabled$`));
    await expect(page.locator('[data-fragment="page-header"]')).toContainText('Active');
  });

  test('edits the grant tree, applies it at once and refuses a page that is out of date', async ({
    page,
    browser,
    makeUser,
    scratch,
  }) => {
    const user = await makeUser('viewer');
    const person = await signedInAs(browser, user);
    await expect(person.locator('#projects-table')).not.toContainText(scratch.projectName);

    await page.goto(`/users/${user.id}`);
    const grants = page.locator('#user-grants');
    await expect(grants.locator('input:checked')).toHaveCount(0);
    await grants.locator('#grant-environment-' + scratch.environmentId).check();
    await saveGrants(page, grants);
    await expect(grants.locator('#grant-environment-' + scratch.environmentId)).toBeChecked();
    await person.reload();
    await expect(person.locator('#projects-table')).toContainText(scratch.projectName);

    const other = await page.context().newPage();
    await other.goto(`/users/${user.id}`);
    await grants.locator('#grant-project-' + scratch.projectId).check();
    await grants.locator('#grant-environment-' + scratch.environmentId).uncheck();
    await saveGrants(page, grants);

    await other.locator('#grant-deployment-' + scratch.deploymentId).check();
    await other.getByRole('button', { name: 'Save grants' }).click();
    const refused = other.locator('#user-grants');
    await expect(refused.getByRole('note')).toContainText('changed since the page was loaded');
    await expect(refused.getByRole('note')).toContainText('The list shows the current grants.');
    await expect(refused.locator('#grant-project-' + scratch.projectId)).toBeChecked();
    await expect(refused.locator('#grant-deployment-' + scratch.deploymentId)).not.toBeChecked();
    await other.close();

    await grants.locator('#grant-project-' + scratch.projectId).uncheck();
    await saveGrants(page, grants);
    await person.reload();
    await expect(person.locator('#projects-table')).not.toContainText(scratch.projectName);
    await person.context().close();
  });

  test('says superadmins hold no grants', async ({ page }) => {
    await page.goto('/users');
    await row(page, 'root').getByRole('link', { name: 'Edit' }).click();
    await expect(page.locator('#user-grants')).toContainText(
      'Superadmins reach every project, so grants do not apply to this account.',
    );
    await expect(page.locator('#user-grants input[type="checkbox"]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Save grants' })).toHaveCount(0);
  });

  test('keeps the page within the viewport at every width', async ({ page, makeUser }) => {
    const user = await makeUser();
    for (const url of ['/users', `/users/${user.id}`]) {
      await page.goto(url);
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    }
    if (!isWide(page)) {
      await page.goto('/users');
      const edit = row(page, user.name).getByRole('link', { name: 'Edit' });
      expect((await edit.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
    }
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

    test('has no Users link and is refused every users page and action', async ({ page }) => {
      await page.goto('/projects');
      await expect(page.locator('nav[aria-label="Primary"] a[href="/users"]')).toHaveCount(0);
      for (const url of ['/users', '/users/table', '/users/new', '/users/1', '/users/1/confirm/disable']) {
        const response = await page.request.get(url);
        expect(response.status(), url).toBe(403);
      }
      const forbidden = await page.goto('/users');
      expect(forbidden?.status()).toBe(403);
      await expect(page.locator('main')).not.toContainText('e2e-u-');
      const headers = await csrfHeaders(page.request);
      for (const [url, form] of [
        ['/users', { name: 'e2e-refused', email: 'e2e-refused@example.test', group: 'viewer' }],
        ['/users/1/disable', { from: 'list' }],
        ['/users/1/enable', { from: 'list' }],
        ['/users/1/group', { group: 'viewer' }],
        ['/users/1/grants', { base: '' }],
      ] as const) {
        const response = await page.request.post(url, { form, headers });
        expect(response.status(), url).toBe(403);
      }
    });
  });
}
