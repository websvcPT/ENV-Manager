import { createDeployment } from '../deployments-api';
import { createHostFolder, type HostFolder } from '../host-files';
import { horizontalOverflow } from '../viewport';
import { expect, test as scratchTest } from './scratch-project';

/*
 * A deployment's own files. Every spec roots a scratch deployment in a folder of its own below
 * the deployments folder, adds the first file from the deployment page, and deploys it there.
 */

const test = scratchTest.extend<{ host: HostFolder }>({
  host: async ({}, use) => {
    const host = createHostFolder();
    try {
      await use(host);
    } finally {
      host.remove();
    }
  },
});

test.describe('deployment files', () => {
  test('are added from the Files tab, kept apart from the services and deployed', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const id = await createDeployment(request, scratch.id, 'files-dep', host.root);
    await page.goto(`/deployments/${id}/files`);
    const panel = page.locator('[data-fragment="deployment-files"]');
    await expect(panel).toContainText('No deployment files yet');
    await panel.locator('#deployment-files-add').click();
    await expect(page).toHaveURL(/\/services\/\d+\/environments\/new$/);

    await page.locator('#en').fill('.env');
    await page.locator('#tp').fill('.env');
    await page.locator('#content').fill('STACK=files\n');
    await page.locator('#env-form').getByRole('button', { name: 'Add environment' }).click();
    await expect(page.locator('[data-fragment="page-header"] [data-item-name]')).toHaveText(
      'Deployment files',
    );
    await expect(page.locator('[data-fragment="page-header"] h1')).not.toContainText('Service:');
    await expect(page.locator('#service-rename-open, #service-delete-open')).toHaveCount(0);

    await page.goto(`/deployments/${id}/files`);
    await expect(panel.locator('[data-fragment="deployment-files-row"]')).toContainText('1 file');
    await expect(page.locator('[data-fragment="services-table"]')).not.toContainText(
      'Deployment files',
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.locator('[data-fragment="deployment-files-row"] a').first().click();
    await page.getByRole('button', { name: 'Deploy deployment files' }).click();
    const dialog = page.locator('#deploy-dialog');
    await expect(dialog.locator('[data-fragment="preflight-table"]')).toBeVisible();
    await dialog.locator('[data-deploy-submit]').click();
    await expect(dialog.locator('[data-fragment="deploy-result"]')).toBeVisible();
    await expect.poll(() => host.read('.env')).toBe('STACK=files\n');
  });
});
