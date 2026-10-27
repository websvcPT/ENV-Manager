import type { Browser, Page } from '@playwright/test';

import { createDeployment as createDeploymentByApi, deploymentId } from '../deployments-api';
import { stateFile, SUPERADMIN_STATE } from '../global-setup';
import { createHostFolder, type HostFolder } from '../host-files';
import { expectBreadcrumb, expectItemHeader, expectLocation } from '../item-header';
import { listProjects } from '../projects-api';
import { horizontalOverflow, isWide } from '../viewport';
import { actionControls, rowAction } from './row-actions';
import { expect, test } from './scratch-project';

const ALLOWED_ROOT = '/srv/deployments';
const SAMPLE_A = `${ALLOWED_ROOT}/sample-a`;
const SAMPLE_B = `${ALLOWED_ROOT}/sample-b`;

async function seededProjectId(browser: Browser, name: string): Promise<number> {
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

function folderForm(projectId: number): string {
  return `/projects/${projectId}/deployments/new?source=folder`;
}

function form(page: Page) {
  return page.locator('#deployment-form');
}

function rootLines(page: Page) {
  return page.locator('#root-v li');
}

function picker(page: Page) {
  return page.locator('#path-picker');
}

async function fillCreateForm(page: Page, name: string, root: string): Promise<void> {
  await page.locator('#name').fill(name);
  await page.locator('#root').fill(root);
}

async function createDeployment(page: Page, projectId: number, name: string, root: string) {
  await page.goto(folderForm(projectId));
  await fillCreateForm(page, name, root);
  await form(page).getByRole('button', { name: 'Create deployment' }).click();
  await expect(page).toHaveURL(/\/deployments\/\d+\/files\?saved=true$/);
  return Number(/\/deployments\/(\d+)\/files\?saved=true$/.exec(page.url())?.[1]);
}

function fillOf(page: Page, radioName: string, value: string) {
  return page
    .locator(`input[name="${radioName}"][value="${value}"]`)
    .locator('xpath=ancestor::label')
    .evaluate((label) => getComputedStyle(label).backgroundColor);
}

test.describe('superadmin create', () => {
  test('shows the empty form with its sections, defaults and picker entry', async ({
    page,
    scratch,
  }) => {
    await page.goto(folderForm(scratch.id));
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('New deployment');
    await expect(page.locator('[data-fragment="page-header"] p')).toHaveText(
      'Post-deploy commands are configured per service, not here.',
    );
    await expectBreadcrumb(page, ['Projects', [scratch.name, 'Project'], 'New deployment']);
    await expect(form(page)).toHaveAttribute('hx-post', `/projects/${scratch.id}/deployments`);
    await expect(form(page).locator('[data-fragment="form-section"] h2')).toHaveText([
      'General',
      'Deploy policies',
      'File defaults',
    ]);
    await expect(page.locator('#name')).toHaveValue('');
    await expect(page.locator('#root')).toHaveValue('');
    await expect(page.locator('#compose_project')).toHaveValue('');
    await expect(page.locator('#compose_project')).not.toHaveAttribute('readonly', '');
    await expect(page.locator('#mode')).toHaveValue('0600');
    await expect(page.locator('input[name="dist_missing_policy"]:checked')).toHaveValue('block');
    await expect(page.locator('input[name="drift_policy"]:checked')).toHaveValue('halt');
    await expect(page.locator('input[name="missing_target_policy"]:checked')).toHaveValue('create');
    await expect(rootLines(page)).toHaveText([
      'Choose a path under an allowed path. It is validated as you type.',
    ]);
    await expect(page.getByRole('link', { name: 'Cancel' })).toHaveAttribute(
      'href',
      `/projects/${scratch.id}`,
    );
    if (!isWide(page)) {
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    }
  });

  test('validates the root path as the person types', async ({ page, scratch }) => {
    await page.goto(folderForm(scratch.id));
    const root = page.locator('#root');

    await root.fill('/etc');
    await expect(rootLines(page)).toHaveCount(1);
    await expect(rootLines(page).first()).toContainText('Path is outside the allowed paths (');
    await expect(rootLines(page).first()).toContainText(ALLOWED_ROOT);
    await expect(rootLines(page).first()).not.toContainText('exist');
    await expect(rootLines(page).first()).toHaveClass(/text-err-fg/);

    await root.fill(SAMPLE_A);
    await expect(rootLines(page)).toHaveText([
      `Inside allowed path ${ALLOWED_ROOT}`,
      'Exists, mounted and writable',
    ]);
    await expect(rootLines(page).first()).toHaveClass(/text-ok-fg/);

    await root.fill(`${ALLOWED_ROOT}/missing-folder`);
    await expect(rootLines(page)).toHaveText([
      `Inside allowed path ${ALLOWED_ROOT}`,
      'Path does not exist.',
    ]);

    await root.fill(`${ALLOWED_ROOT}/plain-file.txt`);
    await expect(rootLines(page)).toHaveText([
      `Inside allowed path ${ALLOWED_ROOT}`,
      'Path is not a directory.',
    ]);

    await root.fill('relative/path');
    await expect(rootLines(page)).toHaveText(['Path must be absolute.']);

    await root.fill('');
    await expect(rootLines(page)).toHaveText([
      'Choose a path under an allowed path. It is validated as you type.',
    ]);
  });

  test('filters the listed folders as you type and opens the only match with Enter', async ({
    page,
    scratch,
  }) => {
    await page.goto(folderForm(scratch.id));
    await page.getByRole('button', { name: 'Browse…' }).click();
    const filter = picker(page).locator('[data-js="path-filter"]');
    await expect(filter).toBeVisible();
    const shown = picker(page).locator('[data-path-entry]:visible');
    await filter.fill('SAMPLE');
    await expect(shown).toHaveCount(2);
    await expect(shown.nth(0)).toContainText(SAMPLE_A);
    await expect(shown.nth(1)).toContainText(SAMPLE_B);
    await expect(picker(page).locator('#path-filter-count')).toHaveText('2 folders shown');
    await filter.fill('no-folder-has-this-name');
    await expect(shown).toHaveCount(0);
    await expect(picker(page).getByText('No folder matches.')).toBeVisible();
    await filter.fill('sample-a');
    await expect(shown).toHaveCount(1);
    await expect(picker(page).getByText('No folder matches.')).toBeHidden();
    await filter.press('Enter');
    await expect(picker(page).locator('[data-fragment="path-picker"]')).toContainText(
      `Folder ${SAMPLE_A}`,
    );
    // The filter starts empty again inside the folder that was opened.
    await expect(picker(page).locator('[data-js="path-filter"]')).toHaveValue('');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('browses inside the allowed paths only and fills the field', async ({ page, scratch }) => {
    await page.goto(folderForm(scratch.id));
    await page.getByRole('button', { name: 'Browse…' }).click();
    await expect(picker(page)).toBeVisible();
    await expect(picker(page).locator('h2')).toHaveText('Choose root path');
    const entries = picker(page).locator('nav[aria-label="Allowed paths"] li button');
    await expect(entries.filter({ hasText: ALLOWED_ROOT }).first()).toContainText('allowed root');
    await expect(entries.filter({ hasText: SAMPLE_A })).toHaveCount(1);
    await expect(entries.filter({ hasText: SAMPLE_B })).toHaveCount(1);
    await expect(entries.filter({ hasText: 'plain-file.txt' })).toHaveCount(0);
    await expect(picker(page).getByRole('button', { name: 'Use this path' })).toBeDisabled();

    await entries.filter({ hasText: SAMPLE_A }).click();
    await expect(picker(page).locator('[data-fragment="path-picker"]')).toContainText(
      `Folder ${SAMPLE_A}`,
    );
    await expect(entries.filter({ hasText: `${SAMPLE_A}/nested` })).toHaveCount(1);
    await expect(entries.filter({ hasText: 'Up one level' })).toHaveCount(1);

    await entries.filter({ hasText: `${SAMPLE_A}/nested` }).click();
    await expect(picker(page)).toContainText('No folders inside this one.');
    await entries.filter({ hasText: 'Up one level' }).click();
    await expect(picker(page)).toContainText(`Folder ${SAMPLE_A}`);

    await picker(page).getByRole('button', { name: 'Use this path' }).click();
    await expect(picker(page)).toBeHidden();
    await expect(page.locator('#root')).toHaveValue(SAMPLE_A);
    await expect(rootLines(page)).toHaveText([
      `Inside allowed path ${ALLOWED_ROOT}`,
      'Exists, mounted and writable',
    ]);

    await page.getByRole('button', { name: 'Browse…' }).click();
    await picker(page).getByRole('button', { name: 'Cancel' }).click();
    await expect(picker(page)).toBeHidden();
    await expect(page.locator('#root')).toHaveValue(SAMPLE_A);
    if (!isWide(page)) {
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    }
  });

  test('policy choice moves the highlighted option', async ({ page, scratch }) => {
    await page.goto(folderForm(scratch.id));
    const selected = await fillOf(page, 'drift_policy', 'halt');
    const plain = await fillOf(page, 'drift_policy', 'overwrite');
    expect(selected).not.toBe(plain);
    await page.getByLabel('Back up, then overwrite').check();
    expect(await fillOf(page, 'drift_policy', 'halt')).toBe(plain);
    expect(await fillOf(page, 'drift_policy', 'backup_overwrite')).toBe(selected);
  });

  test('creates a deployment and lands on its page', async ({ page, scratch }) => {
    await page.goto(folderForm(scratch.id));
    await fillCreateForm(page, 'created-one', SAMPLE_A);
    await page.locator('input[name="drift_policy"][value="overwrite"]').check();
    await page.getByLabel('Warn and continue').check();
    await page.locator('#mode').fill('640');
    await page.locator('#owner').fill('www-data');
    await page.locator('#group').fill('www-data');
    await form(page).getByRole('button', { name: 'Create deployment' }).click();

    await expect(page).toHaveURL(/\/deployments\/\d+\/files\?saved=true$/);
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Deployment saved');
    await expectItemHeader(page, 'deployment', 'created-one');
    await expectLocation(page, 'deployment', {
      root: `Deployment path: ${SAMPLE_A}`,
    });
    const id = /\/deployments\/(\d+)\//.exec(page.url())?.[1];
    await page.goto(`/deployments/${id}/settings`);
    await expect(page.locator('[data-compose-project]')).toHaveText('Not set');
    const policies = page.locator('[data-fragment="policies-panel"]');
    await expect(policies).toContainText('Warn and continue');
    await expect(policies).toContainText('Overwrite');
    await expect(policies).toContainText('0640, www-data:www-data');
    await page.goto(`/projects/${scratch.id}`);
    await expect(
      page.locator('#deployments-table [data-fragment="deployment-row"] th[scope="row"] a'),
    ).toHaveText(['created-one']);
  });

  test('refuses bad values with every message and keeps what was typed', async ({
    page,
    scratch,
  }) => {
    await createDeployment(page, scratch.id, 'taken-name', SAMPLE_A);
    await page.goto(folderForm(scratch.id));
    await fillCreateForm(page, 'taken-name', '/etc');
    await page.locator('#mode').fill('12');
    await page.locator('input[name="drift_policy"][value="overwrite"]').check();
    await form(page).getByRole('button', { name: 'Create deployment' }).click();

    await expect(page).toHaveURL(folderForm(scratch.id));
    await expect(page.locator('#name-e')).toHaveText(
      'A deployment named taken-name already exists in this project.',
    );
    await expect(page.locator('#name')).toHaveValue('taken-name');
    await expect(page.locator('#root')).toHaveValue('/etc');
    await expect(page.locator('#root')).toHaveAttribute('aria-invalid', 'true');
    await expect(rootLines(page)).toHaveCount(1);
    await expect(rootLines(page).first()).toContainText('Path is outside the allowed paths');
    await expect(page.locator('#mode-e')).toHaveText('Use an octal mode such as 0600.');
    await expect(page.locator('input[name="drift_policy"]:checked')).toHaveValue('overwrite');
    await expect(page.locator('input[name="dist_missing_policy"]:checked')).toHaveValue('block');
    await expect(page.locator('input[name="missing_target_policy"]:checked')).toHaveValue('create');

    await page.locator('#name').fill('');
    await page.locator('#root').fill('');
    await page.locator('#mode').fill('0600');
    await form(page).getByRole('button', { name: 'Create deployment' }).click();
    await expect(page.locator('#name-e')).toHaveText('Enter a deployment name.');
    await expect(rootLines(page)).toHaveText(['Enter a root path.']);
    await expect(page.locator('#deployment-form')).toHaveCount(1);
  });
});

test.describe('superadmin edit', () => {
  test('opens prefilled with the root checks and saves the changes', async ({ page, scratch }) => {
    const id = await createDeployment(page, scratch.id, 'edit-me', SAMPLE_A);
    await page.goto(`/deployments/${id}/edit`);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('Edit edit-me');
    await expectBreadcrumb(page, ['Projects', [scratch.name, 'Project'], 'Edit edit-me']);
    await expect(form(page)).toHaveAttribute('hx-post', `/deployments/${id}/edit`);
    await expect(page.locator('#name')).toHaveValue('edit-me');
    await expect(page.locator('#root')).toHaveValue(SAMPLE_A);
    await expect(rootLines(page)).toHaveText([
      `Inside allowed path ${ALLOWED_ROOT}`,
      'Exists, mounted and writable',
    ]);
    await expect(page.locator('input[name="drift_policy"]:checked')).toHaveValue('halt');
    await expect(form(page).getByRole('button', { name: 'Save deployment' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Cancel' })).toHaveAttribute(
      'href',
      `/deployments/${id}/settings`,
    );

    await expect(page.locator('#name-h')).toContainText('Letters, numbers, spaces');
    await page.locator('#name').fill('  Edited   Name EU ');
    await page.locator('#root').fill(SAMPLE_B);
    await page.getByLabel('Back up, then overwrite').check();
    await page.locator('#group').fill('deploy');
    await expect(page.locator('#compose_project')).toHaveValue('');
    await page.locator('#compose_project').fill('Bad Name');
    await form(page).getByRole('button', { name: 'Save deployment' }).click();
    await expect(page.locator('#compose_project-e')).toContainText('lowercase letters');
    await page.locator('#compose_project').fill('edited-stack');
    await form(page).getByRole('button', { name: 'Save deployment' }).click();

    await expect(page).toHaveURL(`/deployments/${id}/settings?saved=true`);
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Deployment saved');
    await expectItemHeader(page, 'deployment', 'Edited Name EU');
    await expect(page.locator('[data-location="root"]')).toHaveText(`Deployment path: ${SAMPLE_B}`);
    await expect(page.locator('[data-compose-project]')).toHaveText('edited-stack');
    await expect(page.locator('[data-fragment="policies-panel"]')).toContainText(
      'Back up, then overwrite',
    );
    await expect(page.locator('[data-fragment="policies-panel"]')).toContainText(':deploy');
  });

  test('refuses an edit that leaves the allowed paths', async ({ page, scratch }) => {
    const id = await createDeployment(page, scratch.id, 'stay-inside', SAMPLE_A);
    await page.goto(`/deployments/${id}/edit`);
    await page.locator('#root').fill('/etc');
    await form(page).getByRole('button', { name: 'Save deployment' }).click();
    await expect(page).toHaveURL(`/deployments/${id}/edit`);
    await expect(rootLines(page)).toHaveCount(1);
    await expect(rootLines(page).first()).toContainText('Path is outside the allowed paths');
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('[data-location="root"]')).toHaveText(`Deployment path: ${SAMPLE_A}`);
  });

  test('a deployment outside the allowed paths shows its error and must move', async ({
    page,
    browser,
  }) => {
    const legacy = await deploymentId(browser, 'billing', 'legacy');
    await page.goto(`/deployments/${legacy}/edit`);
    await expect(page.locator('#root')).toHaveValue('/srv/old/legacy');
    await expect(rootLines(page)).toHaveCount(1);
    await expect(rootLines(page).first()).toContainText('Path is outside the allowed paths');
    await expect(page.locator('#root')).not.toHaveAttribute('aria-invalid', 'true');
    await form(page).getByRole('button', { name: 'Save deployment' }).click();
    await expect(page).toHaveURL(`/deployments/${legacy}/edit`);
    await expect(page.locator('#root')).toHaveAttribute('aria-invalid', 'true');
  });
});

for (const user of ['admin', 'viewer'] as const) {
  test.describe(`${user} is refused`, () => {
    test.use({ storageState: stateFile(user) });

    test('the form pages and the path helpers answer 403', async ({
      page,
      browser,
      request,
    }) => {
      const fancy = await seededProjectId(browser, 'Fancy Proj. Name');
      const prod = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
      for (const url of [`/projects/${fancy}/deployments/new`, `/deployments/${prod}/edit`]) {
        const response = await page.goto(url);
        expect(response?.status()).toBe(403);
      }
      expect((await request.get('/paths/validate?root=/etc')).status()).toBe(403);
      expect((await request.get('/paths/browse')).status()).toBe(403);
      for (const tail of ['stacks', 'stacks/loading', 'use?folder=/srv/deployments']) {
        const response = await request.get(`/projects/${fancy}/deployments/new/${tail}`);
        expect(response.status(), tail).toBe(403);
      }
      await page.goto(`/projects/${fancy}`);
      await expect(page.getByRole('link', { name: 'New deployment' })).toHaveCount(0);
    });
  });
}

const stacksTest = test.extend<{ host: HostFolder }>({
  host: async ({}, use) => {
    const host = createHostFolder();
    try {
      host.write('compose.yaml', 'services:\n  web:\n    image: nginx:1.27\n');
      await use(host);
    } finally {
      host.remove();
    }
  },
});

function stacksPage(projectId: number): string {
  return `/projects/${projectId}/deployments/new`;
}

/** The table row or list item of the stack in a folder, whichever the layout shows. */
function stackEntry(page: Page, folder: string) {
  return page.locator(`[data-folder="${folder}"]:visible`);
}

async function stacksLoaded(page: Page): Promise<void> {
  await expect(page.locator('#detected-stacks')).toHaveAttribute('aria-busy', 'false');
}

async function useStack(page: Page, folder: string): Promise<void> {
  await (await rowAction(stackEntry(page, folder), 'use')).click();
  await expect(page.locator('#stack-form #deployment-form')).toBeVisible();
}

function baseName(folder: string): string {
  return folder.slice(folder.lastIndexOf('/') + 1);
}

test.describe('new deployment from a detected stack', () => {
  stacksTest('shows the loading state, then the stacks found in the allowed paths', async ({
    page,
    scratch,
    host,
  }) => {
    await page.route('**/deployments/new/stacks', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 600));
      await route.continue();
    });
    await page.goto(stacksPage(scratch.id));
    await expect(page.locator('nav[aria-label="Sections"] [aria-current="page"]')).toHaveText(
      'Detected stacks',
    );
    await expect(page.locator('#detected-stacks')).toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('#detected-stacks [role="status"]')).toHaveText(
      'Looking for stacks',
    );
    await expect(page.locator('#deployment-form')).toHaveCount(0);
    await stacksLoaded(page);

    const entry = stackEntry(page, host.root);
    await expect(entry).toContainText(baseName(host.root));
    await expect(entry).toContainText('compose.yaml');
    await expect(entry).toContainText(isWide(page) ? 'Folder' : 'Found by folder');
    await expect(entry).toContainText('Not in Docker');
    await expect(page.locator('#detected-stacks')).toContainText('Docker was not used');
    await expect(page.locator('#detected-stacks-scan')).toBeVisible();
    // The e2e stack has no Docker socket, so every stack comes from the folders.
    if (isWide(page)) {
      await expect(page.locator('[data-fragment="stack-row"]').first()).toBeVisible();
      await expect(page.locator('[data-fragment="stack-item"]').first()).toBeHidden();
    } else {
      await expect(page.locator('[data-fragment="stack-item"]').first()).toBeVisible();
      await expect(page.locator('[data-fragment="stack-row"]').first()).toBeHidden();
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    }
  });

  stacksTest('Use fills the form and Create lands on the import with the file ticked', async ({
    page,
    scratch,
    host,
  }) => {
    await page.goto(stacksPage(scratch.id));
    await stacksLoaded(page);
    await useStack(page, host.root);

    await expect(page.locator('#name')).toHaveValue(baseName(host.root));
    await expect(page.locator('#root')).toHaveValue(host.root);
    await expect(rootLines(page)).toHaveText([
      `Inside allowed path ${ALLOWED_ROOT}`,
      'Exists, mounted and writable',
    ]);
    await expect(page.locator('[data-fragment="stack-files"] li')).toHaveText(['compose.yaml']);
    await expect(page.locator('#compose_project')).toHaveValue('');
    await expect(page.locator('#compose_project')).not.toHaveAttribute('readonly', '');
    await expect(page.locator('input[name="import_next"]')).toBeChecked();
    await expect(page.getByRole('button', { name: 'Browse…' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Cancel' })).toHaveAttribute(
      'href',
      `/projects/${scratch.id}`,
    );
    if (!isWide(page)) {
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    }

    await page.locator('#compose_project').fill('picked-stack');
    await form(page).getByRole('button', { name: 'Create deployment' }).click();
    await expect(page).toHaveURL(/\/deployments\/\d+\/import-compose\?compose_file=compose\.yaml$/);
    await expect(page.locator('input[name="compose_file"][value="compose.yaml"]')).toBeChecked();
  });

  stacksTest('without the import it lands on Files with the Compose file linked', async ({
    page,
    scratch,
    host,
  }) => {
    await page.goto(stacksPage(scratch.id));
    await stacksLoaded(page);
    await useStack(page, host.root);
    await page.locator('input[name="import_next"]').uncheck();
    await page.locator('#compose_project').fill('picked-stack');
    await form(page).getByRole('button', { name: 'Create deployment' }).click();

    await expect(page).toHaveURL(/\/deployments\/\d+\/files\?saved=true$/);
    await expect(page.locator('[data-fragment="toast"]')).toContainText('Deployment saved');
    const id = /\/deployments\/(\d+)\//.exec(page.url())?.[1];
    await page.goto(`/deployments/${id}/settings`);
    await expect(page.locator('[data-compose-project]')).toHaveText('picked-stack');
    await expect(page.locator('[data-fragment="compose-file-row"]')).toHaveAttribute(
      'data-file',
      'compose.yaml',
    );
  });

  stacksTest('a refused submit keeps the picked files and says what is wrong', async ({
    page,
    scratch,
    host,
  }) => {
    await page.goto(stacksPage(scratch.id));
    await stacksLoaded(page);
    await useStack(page, host.root);
    await page.locator('#compose_project').fill('Not Valid');
    await form(page).getByRole('button', { name: 'Create deployment' }).click();
    await expect(page.locator('#compose_project-e')).toContainText('lowercase letters');
    await expect(page.locator('[data-fragment="stack-files"] li')).toHaveText(['compose.yaml']);
    await expect(page.locator('input[name="import_next"]')).toBeChecked();

    await page.locator('#compose_project').fill('');
    await page.locator('#root').fill(SAMPLE_A);
    await form(page).getByRole('button', { name: 'Create deployment' }).click();
    await expect(page.locator('#compose-files-e')).toContainText('does not exist');
    await expect(page.locator('#deployment-form')).toHaveCount(1);
  });

  stacksTest('a stack that a deployment uses links to it and has no Use', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const id = await createDeploymentByApi(request, scratch.id, 'already-here', host.root);
    await page.goto(stacksPage(scratch.id));
    await stacksLoaded(page);
    const entry = stackEntry(page, host.root);
    await expect(entry.locator('[data-stack-used]')).toHaveAttribute('href', `/deployments/${id}`);
    await expect(entry.locator('[data-stack-used]')).toHaveText('Used by already-here');
    await expect(actionControls(entry, 'use')).toHaveCount(0);
  });

  stacksTest('Scan again looks for the stacks once more', async ({ page, scratch, host }) => {
    await page.goto(stacksPage(scratch.id));
    await stacksLoaded(page);
    const later = createHostFolder();
    try {
      later.write('compose.yaml', 'services: {}\n');
      await expect(stackEntry(page, later.root)).toHaveCount(0);
      await page.locator('#detected-stacks-scan').click();
      await expect(stackEntry(page, later.root)).toBeVisible();
      await expect(stackEntry(page, host.root)).toBeVisible();
    } finally {
      later.remove();
    }
  });

  stacksTest('the switch opens the folder form and back', async ({ page, scratch }) => {
    await page.goto(stacksPage(scratch.id));
    await page.locator('nav[aria-label="Sections"] a', { hasText: 'Enter the folder' }).click();
    await expect(page).toHaveURL(folderForm(scratch.id));
    await expect(form(page)).toBeVisible();
    await expect(page.locator('#detected-stacks')).toHaveCount(0);
    await page.locator('nav[aria-label="Sections"] a', { hasText: 'Detected stacks' }).click();
    await expect(page.locator('#detected-stacks')).toBeVisible();
    await expect(page.locator('#deployment-form')).toHaveCount(0);
  });
});
