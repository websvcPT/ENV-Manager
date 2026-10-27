import type { Locator, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createHostFolder, type HostFolder } from '../host-files';
import { horizontalOverflow } from '../viewport';
import { rowAction } from './row-actions';
import { expect, test as scratchTest } from './scratch-project';

/*
 * The Compose files panel of a deployment. Every spec roots a scratch deployment in a folder of
 * its own below the deployments folder, with a Compose file and an override beside it.
 */

const COMPOSE = 'services:\n  web:\n    image: nginx\n';
const OVERRIDE = 'services:\n  web:\n    labels:\n      note: "<b>x</b>"\n';

const test = scratchTest.extend<{ host: HostFolder }>({
  host: async ({}, use) => {
    const host = createHostFolder();
    try {
      host.write('compose.yaml', COMPOSE);
      host.write('compose.override.yaml', OVERRIDE);
      await use(host);
    } finally {
      host.remove();
    }
  },
});

function panel(page: Page): Locator {
  return page.locator('section[data-fragment="compose-files"]');
}

function rows(page: Page): Locator {
  return panel(page).locator('tr[data-fragment="compose-file-row"]');
}

function toast(page: Page): Locator {
  return page.locator('[data-fragment="toast"]');
}

async function openLink(page: Page): Promise<Locator> {
  await page.locator('#compose-link-open').click();
  const form = page.locator('#compose-link [data-fragment="compose-link"]');
  await expect(form).toBeVisible();
  return form;
}

test.describe('Compose files of a deployment', () => {
  test('are linked, read and unlinked by the superadmin', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const id = await createDeployment(request, scratch.id, 'compose-view-dep', host.root);
    await page.goto(`/deployments/${id}/settings`);
    await expect(panel(page)).toContainText('No Compose files linked');
    await expect(page.locator('#compose-link-open')).toHaveText(/Link Compose files/);

    // Link both files through the dialog.
    let form = await openLink(page);
    const offers = form.locator('[data-fragment="compose-offer"]');
    await expect(offers).toHaveCount(2);
    await form.locator('input[name="compose_file"][value="compose.yaml"]').check();
    await form.locator('input[name="compose_file"][value="compose.override.yaml"]').check();
    await form.locator('#compose-link-submit').click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${id}/settings\\?compose_linked=2,0$`));
    await expect(toast(page)).toContainText('2 Compose files linked.');
    await expect(rows(page)).toHaveCount(2);
    await expect(rows(page).nth(0)).toHaveAttribute('data-file', 'compose.yaml');
    await expect(rows(page).nth(0)).toContainText('Base');
    await expect(rows(page).nth(1)).toHaveAttribute('data-file', 'compose.override.yaml');
    await expect(rows(page).nth(1)).toContainText('Override');
    await expect(page.locator('#compose-link-open')).toHaveText(/Change links/);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    // The text of a file is shown as text, never as markup.
    const over = rows(page).nth(1);
    await (await rowAction(over, 'view')).click();
    const view = page.locator('#compose-view');
    await expect(view).toBeVisible();
    const text = view.locator('[data-fragment="compose-file-view"] [data-compose-text] pre');
    await expect(text).toContainText('<b>x</b>');
    await expect(text.locator('b')).toHaveCount(0);
    await view.locator('[data-js="dialog-close"]').last().click();
    await expect(view).toBeHidden();

    const base = rows(page).nth(0);
    await (await rowAction(base, 'view')).click();
    await expect(view.locator('[data-compose-text] pre')).toContainText('image: nginx');
    await view.locator('[data-js="dialog-close"]').last().click();
    await expect(view).toBeHidden();

    // A refused path shows the reason and keeps the dialog.
    form = await openLink(page);
    await form.locator('#cl-extra').fill('../x.yml');
    await form.locator('#compose-link-submit').click();
    const dialog = page.locator('#compose-link');
    await expect(dialog.locator('[role="alert"]')).toContainText(
      'The Compose files cannot be linked',
    );
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('#cl-extra')).toHaveValue('../x.yml');
    await expect(rows(page)).toHaveCount(2);

    // Unticking every file unlinks them.
    await dialog.locator('#cl-extra').fill('');
    for (const box of await dialog.locator('input[name="compose_file"]').all()) {
      await box.uncheck();
    }
    await dialog.locator('#compose-link-submit').click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${id}/settings\\?compose_linked=0,0$`));
    await expect(toast(page)).toContainText('Compose files unlinked.');
    await expect(rows(page)).toHaveCount(0);
    await expect(panel(page)).toContainText('No Compose files linked');
    await expect(page.locator('#compose-link-open')).toHaveText(/Link Compose files/);
  });

  test('show on the Deployment files page without the link button', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const id = await createDeployment(request, scratch.id, 'compose-files-dep', host.root);
    await page.goto(`/deployments/${id}/settings`);
    const form = await openLink(page);
    await form.locator('input[name="compose_file"][value="compose.yaml"]').check();
    await form.locator('#compose-link-submit').click();
    await expect(toast(page)).toContainText('1 Compose file linked.');
    await expect(rows(page)).toHaveCount(1);

    await page.goto(`/deployments/${id}/files`);
    const files = page.locator('[data-fragment="deployment-files"]');
    await files.locator('#deployment-files-add').click();
    await expect(page).toHaveURL(/\/services\/\d+\/environments\/new$/);
    await page.locator('#en').fill('.env');
    await page.locator('#tp').fill('.env');
    await page.locator('#content').fill('STACK=files\n');
    await page.locator('#env-form').getByRole('button', { name: 'Add environment' }).click();
    await expect(page.locator('[data-fragment="page-header"] [data-item-name]')).toHaveText(
      'Deployment files',
    );

    await page.goto(`/deployments/${id}/files`);
    await page.locator('[data-fragment="deployment-files-row"] a').first().click();
    await expect(page).toHaveURL(/\/services\/\d+$/);
    await expect(panel(page)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toHaveAttribute('data-file', 'compose.yaml');
    await expect(page.locator('#compose-link-open')).toHaveCount(0);
    await (await rowAction(rows(page).first(), 'view')).click();
    await expect(page.locator('#compose-view [data-compose-text] pre')).toContainText(
      'image: nginx',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});
