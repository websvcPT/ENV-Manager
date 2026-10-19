import { expect, test as base, type Locator, type Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment } from '../environments-api';
import { stateFile } from '../global-setup';
import { csrfHeaders } from '../http';
import { createProject, deleteProjectIfPresent } from '../projects-api';
import { createService } from '../services-api';
import { createToken } from '../tokens-api';
import { createUser, disableUser, uniqueUserName, type CreatedUser } from '../users-api';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';
import { rowAction } from './row-actions';
import { signedInAs } from './sign-in';

/*
 * API tokens. Tokens cannot be deleted and a superadmin cannot create one for someone else, so
 * every token here belongs to a scratch admin with a grant on a scratch project, and the scratch
 * admin is disabled when the test ends, which revokes the tokens (and lets the scratch project
 * be deleted again). The seeded billing-ci token is only read, never revoked: other specs use it.
 */

const SAMPLE_A = '/srv/deployments/sample-a';
const SECRET_PATTERN = /^envm_[0-9a-f]{48}$/;

interface TokenOwner {
  user: CreatedUser;
  projectName: string;
  /** A browser page of its own, signed in as the scratch admin with this project's layout. */
  page: Page;
}

/**
 * The base test plus `owner`: a scratch project with one deployment, service and file, a scratch
 * admin granted that project and a page signed in as that admin. Teardown disables the admin
 * first (which revokes their tokens) and then deletes the project.
 */
const test = base.extend<{ owner: TokenOwner }>({
  owner: async ({ request, browser }, use, testInfo) => {
    const suffix = Math.random().toString(36).slice(2, 7);
    const projectName = `e2e-detail-${Date.now()}-${suffix}-${testInfo.project.name}`;
    const projectId = await createProject(request, projectName);
    const deploymentId = await createDeployment(request, projectId, 'scratch-dep', SAMPLE_A);
    const serviceId = await createService(request, deploymentId, 'cache');
    await createEnvironment(request, serviceId, '.env.cache', 'A=1\n');
    const user = await createUser(request, uniqueUserName(testInfo.project.name), 'admin', [
      `project:${projectId}`,
    ]);
    const page = await signedInAs(browser, user);
    try {
      await use({ user, projectName, page });
    } finally {
      await page.context().close();
      await disableUser(request, user.id);
      await deleteProjectIfPresent(request, projectId);
    }
  },
});

function tokenName(): string {
  return `e2e-tok-${Math.random().toString(36).slice(2, 8)}`;
}

function row(page: Page, name: string): Locator {
  return page.locator('#tokens-table [data-fragment="token-row"]', {
    has: page.locator('th[scope="row"] span.font-mono', { hasText: name }),
  });
}

async function openCreate(page: Page): Promise<Locator> {
  await page.locator('[data-js="dialog-open"][data-target="token-create"]').first().click();
  const dialog = page.locator('#token-create[open]');
  await expect(dialog.locator('#tn')).toBeVisible();
  return dialog;
}

async function openRevoke(page: Page, name: string): Promise<Locator> {
  await (await rowAction(row(page, name), 'revoke')).click();
  const dialog = page.locator('#token-revoke[open]');
  await expect(dialog.getByRole('heading', { name: `Revoke token ${name}` })).toBeVisible();
  return dialog;
}

function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

test.describe('admin: creating and revoking tokens', () => {
  test('an admin without tokens sees the empty state and the create dialog', async ({ owner }) => {
    const { page } = owner;
    await page.goto('/tokens');
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('API tokens');
    await expect(
      page.getByText('Your tokens. A token acts as you and never exceeds your access.'),
    ).toBeVisible();
    await expect(
      page.locator('nav[aria-label="Primary"] a[aria-current="page"]').first(),
    ).toHaveText(/API tokens/);
    const empty = page.locator('#tokens-table [data-fragment="empty-state"]');
    await expect(empty.getByText('No API tokens')).toBeVisible();
    await expect(empty.getByText('The secret is shown once.')).toBeVisible();
    expect(await horizontalOverflow(page)).toBe(0);

    const dialog = await openCreate(page);
    await expect(dialog.getByRole('heading', { name: 'Create API token' })).toBeVisible();
    await expect(dialog.locator('input[name="operation"]')).toHaveCount(2);
    await expect(dialog.locator('input[name="operation"][value="deploy"]')).toBeChecked();
    await expect(
      dialog.locator('input[name="operation"][value="read_last_deployment"]'),
    ).not.toBeChecked();
    await expect(dialog.getByText('Deploy a service.')).toBeVisible();
    await expect(dialog.getByText('Read the last deploy result of a service.')).toBeVisible();
    await expect(dialog.getByText('Check dist')).toHaveCount(0);
    await expect(dialog.locator('#tp option').first()).toHaveText('All of my projects');
    await expect(
      dialog.locator('#tp optgroup[label="Projects"] option', { hasText: owner.projectName }),
    ).toHaveCount(1);
    await expect(dialog.locator('#te')).toHaveAttribute('type', 'date');
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('#token-create[open]')).toHaveCount(0);
  });

  test('a refused form keeps the choices and shows the reason next to the field', async ({
    owner,
  }) => {
    const { page } = owner;
    await page.goto('/tokens');
    const dialog = await openCreate(page);
    await dialog.locator('#tn').fill('   ');
    await dialog.locator('#tp').selectOption({ label: owner.projectName });
    await dialog.locator('#token-create-form button[type="submit"]').click();
    await expect(dialog.locator('#tn-e')).toHaveText('Enter a name.');
    await expect(dialog.locator('#tp option:checked')).toHaveText(owner.projectName);

    await dialog.locator('#tn').fill(tokenName());
    await dialog.locator('input[name="operation"][value="deploy"]').uncheck();
    await dialog.locator('#token-create-form button[type="submit"]').click();
    await expect(dialog.locator('#to-e')).toHaveText('Choose at least one operation.');
    await expect(dialog.locator('#secret')).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(page.locator('#tokens-table [data-fragment="empty-state"]')).toBeVisible();
  });

  test('the secret is shown once with copy and an example, then lives nowhere in the page', async ({
    owner,
  }) => {
    const { page } = owner;
    const name = tokenName();
    const expires = inDays(30);
    await page.goto('/tokens');
    const dialog = await openCreate(page);
    await dialog.locator('#tn').fill(name);
    await dialog.locator('input[name="operation"][value="read_last_deployment"]').check();
    await dialog.locator('#tp').selectOption({ label: owner.projectName });
    await dialog.locator('#te').fill(expires);
    await dialog.locator('#token-create-form button[type="submit"]').click();

    await expect(dialog.getByRole('heading', { name: 'Token created' })).toBeVisible();
    await expect(dialog.getByText(name, { exact: true })).toBeVisible();
    await expect(
      dialog.getByText('Shown once. It is stored hashed and cannot be shown again.'),
    ).toBeVisible();
    const secret = await dialog.locator('#secret').inputValue();
    expect(secret).toMatch(SECRET_PATTERN);
    await expect(dialog.locator('[data-js="copy"][data-target="secret"]')).toBeVisible();
    const example = dialog.locator('pre');
    await expect(example).toContainText('Authorization: Bearer $ENVM_TOKEN');
    await expect(example).toContainText('/api/v1/services/SERVICE_ID/deploy');
    await expect(example).toContainText('/api/v1/services/SERVICE_ID/deployments/last');
    expect(await example.innerText()).not.toContain(secret);
    expect(await horizontalOverflow(page)).toBe(0);

    await dialog.getByRole('button', { name: 'Done' }).click();
    await expect(page.locator('#token-create[open]')).toHaveCount(0);
    await expect(page.locator('#secret')).toHaveCount(0);
    expect(await page.content()).not.toContain(secret);

    const created = row(page, name);
    await expect(created).toHaveCount(1);
    await expect(created.locator('th[scope="row"]')).toContainText(secret.slice(0, 13));
    await expect(created.locator('td').nth(0)).toHaveText('Deploy, Read last deployment');
    await expect(created.locator('td').nth(1)).toHaveText(owner.projectName);
    await expect(created.locator('td').nth(3)).toHaveText('Never');
    await expect(created.locator('td').nth(4)).toHaveText(expires);
    await expect(created.locator('td').nth(5)).toContainText('Active');
    await expect(created.locator('[data-row-action="revoke"]')).toBeVisible();

    await page.reload();
    expect(await page.content()).not.toContain(secret);
    await openCreate(page);
    await expect(page.locator('#secret')).toHaveCount(0);
    expect(await page.locator('#token-create').innerHTML()).not.toContain(secret);
  });

  test('revoking asks first, stops the token and is audited', async ({ owner }) => {
    const { page } = owner;
    const name = tokenName();
    await createToken(page.context().request, name);
    await page.goto('/tokens');
    await expect(row(page, name).locator('td').nth(5)).toContainText('Active');

    const dialog = await openRevoke(page, name);
    await expect(
      dialog.getByText('The token stops working immediately. Calls made with it stay in the audit log.'),
    ).toBeVisible();
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.locator('#token-revoke[open]')).toHaveCount(0);
    await expect(row(page, name).locator('td').nth(5)).toContainText('Active');

    await openRevoke(page, name);
    await page.locator('#token-revoke button[type="submit"]').click();
    await expect(page.locator('#token-revoke[open]')).toHaveCount(0);
    await expect(row(page, name).locator('td').nth(5)).toContainText('Revoked');
    await expect(row(page, name).locator('[data-row-action="revoke"]')).toHaveCount(0);

    await page.goto('/audit?action=token.revoke');
    const entry = page.locator('[data-fragment="audit-row"]', { hasText: name });
    await expect(entry).toHaveCount(1);
    await expect(entry).toContainText('token.revoke');
    await page.goto('/audit?action=token.create');
    await expect(page.locator('[data-fragment="audit-row"]', { hasText: name })).toHaveCount(1);
  });

  test('the table scrolls inside its panel and Revoke is a touch target on a phone', async ({
    owner,
  }, testInfo) => {
    const { page } = owner;
    expect(isWide(page)).toBe(testInfo.project.name === 'desktop');
    const name = tokenName();
    await createToken(page.context().request, name);
    await page.goto('/tokens');
    expect(await horizontalOverflow(page)).toBe(0);
    const revoke = row(page, name).locator('[data-row-action="revoke"]');
    if (!isWide(page)) {
      const box = await revoke.boundingBox();
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }
    await expect(revoke).toBeAttached();
  });
});

test.describe('superadmin: every owner and the user page', () => {
  test('sees the owner of each token and revokes another user\'s token from the list', async ({
    owner,
    page,
  }) => {
    const name = tokenName();
    await createToken(owner.page.context().request, name);
    await page.goto('/tokens');
    await expect(
      page.getByText('Your tokens and the tokens of other users.'),
    ).toBeVisible();
    await expect(page.locator('#tokens-table th[scope="col"]').nth(1)).toHaveText('Owner');
    const listed = row(page, name);
    await expect(listed.locator('td').first()).toHaveText(owner.user.name);
    await expect(row(page, 'billing-ci')).toHaveCount(1);
    expect(await horizontalOverflow(page)).toBe(0);

    const dialog = await openRevoke(page, name);
    await expect(dialog.getByText(`Owner ${owner.user.name}`)).toBeVisible();
    await page.locator('#token-revoke button[type="submit"]').click();
    await expect(page.locator('#token-revoke[open]')).toHaveCount(0);
    await expect(listed.locator('td').nth(6)).toContainText('Revoked');

    await owner.page.goto('/tokens');
    await expect(row(owner.page, name).locator('td').nth(5)).toContainText('Revoked');
  });

  test('revokes from the tokens panel of the user page, and disabling the user revokes the rest', async ({
    owner,
    page,
  }) => {
    const first = tokenName();
    const second = tokenName();
    const request = owner.page.context().request;
    await createToken(request, first);
    await createToken(request, second);
    await page.goto(`/users/${owner.user.id}`);
    const panel = page.locator('#user-tokens');
    await expect(panel.locator('th[scope="col"]')).toHaveText([
      'Name',
      'Last used',
      'Status',
      'Actions',
    ]);
    const panelRow = (name: string) =>
      panel.locator('[data-fragment="token-row"]', { hasText: name });
    await expect(panelRow(first).getByText('Active')).toBeVisible();

    await (await rowAction(panelRow(first), 'revoke')).click();
    const dialog = page.locator('#token-revoke[open]');
    await expect(dialog.getByRole('heading', { name: `Revoke token ${first}` })).toBeVisible();
    await dialog.locator('button[type="submit"]').click();
    await expect(page.locator('#token-revoke[open]')).toHaveCount(0);
    await expect(panelRow(first).getByText('Revoked')).toBeVisible();
    await expect(panelRow(first).locator('[data-row-action="revoke"]')).toHaveCount(0);
    await expect(panelRow(second).getByText('Active')).toBeVisible();

    await page.getByRole('button', { name: 'Disable' }).first().click();
    const action = page.locator('#user-action[open]');
    await expect(
      action.getByText('All of their API tokens are revoked for good.'),
    ).toBeVisible();
    await action.getByRole('button', { name: 'Disable user' }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${owner.user.id}\\?updated=disabled`));
    await expect(page.getByText('Sessions ended and API tokens revoked.')).toBeVisible();
    await expect(panelRow(second).getByText('Revoked')).toBeVisible();
    await expect(panel.locator('[data-row-action="revoke"]')).toHaveCount(0);

    await page.getByRole('button', { name: 'Enable' }).first().click();
    await expect(
      page.locator('#user-action[open]').getByText('Their old API tokens stay revoked'),
    ).toBeVisible();
    await page.locator('#user-action[open]').getByRole('button', { name: 'Enable user' }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${owner.user.id}\\?updated=enabled`));
    await expect(panelRow(second).getByText('Revoked')).toBeVisible();
  });
});

test.describe('seeded roles', () => {
  test.use({ storageState: stateFile('viewer') });

  test('a viewer has no API tokens link and gets 403 on the page and the dialog', async ({
    page,
  }) => {
    await page.goto('/projects');
    await expect(page.locator('nav[aria-label="Primary"] a[href="/tokens"]')).toHaveCount(0);
    const response = await page.goto('/tokens');
    expect(response?.status()).toBe(403);
    await expect(page.getByRole('heading', { name: 'No access' })).toBeVisible();
    const dialog = await page.request.get('/tokens/new', { headers: { 'HX-Request': 'true' } });
    expect(dialog.status()).toBe(403);
    const create = await page.request.post('/tokens', {
      form: { name: 'sneaky', operation: 'deploy' },
      headers: await csrfHeaders(page.request),
    });
    expect(create.status()).toBe(403);
  });
});

test.describe('seeded admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('lists only their own seeded token and never offers another user\'s', async ({ page }) => {
    await page.goto('/tokens');
    await expect(row(page, 'billing-ci')).toHaveCount(1);
    await expect(row(page, 'billing-ci').locator('td').nth(0)).toHaveText('Deploy');
    await expect(row(page, 'billing-ci').locator('td').nth(1)).toContainText('.env.api');
    await expect(page.locator('#tokens-table th[scope="col"]').nth(1)).toHaveText('Scope');
  });
});

test.describe('admin without tokens', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('sees the empty state, not the tokens of others', async ({ page }) => {
    await page.goto('/tokens');
    await expect(page.locator('#tokens-table [data-fragment="empty-state"]')).toBeVisible();
    await expect(page.getByText('billing-ci')).toHaveCount(0);
  });
});
