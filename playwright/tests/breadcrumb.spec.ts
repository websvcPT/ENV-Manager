import { deploymentId } from '../deployments-api';
import { seededEnvironmentId } from '../environments-api';
import {
  breadcrumbItems,
  breadcrumbTypes,
  compactBreadcrumb,
  expectBreadcrumb,
  expectItemHeader,
  expectLocation,
  itemHeader,
} from '../item-header';
import { TOUCH_TARGET, horizontalOverflow } from '../viewport';
import { expect, test } from './scratch-project';

const PROJECT = 'Fancy Proj. Name';

test.describe('breadcrumb in the top bar', () => {
  test('names the type of each object item and of nothing else @wide', async ({
    page,
    browser,
  }) => {
    const environment = await seededEnvironmentId(browser, PROJECT, 'prod', 'mysql', '.env.db');
    await page.goto(`/environments/${environment}`);
    await expectBreadcrumb(page, [
      'Projects',
      [PROJECT, 'Project'],
      ['prod', 'Deployment'],
      ['mysql', 'Service'],
      ['.env.db', 'Environment'],
    ]);
    await expect(breadcrumbTypes(page)).toHaveText(['Project', 'Deployment', 'Service', 'Environment']);

    await page.goto(`/environments/${environment}/settings`);
    await expectBreadcrumb(page, [
      'Projects',
      [PROJECT, 'Project'],
      ['prod', 'Deployment'],
      ['mysql', 'Service'],
      ['.env.db', 'Environment'],
      'Settings',
    ]);

    await page.goto('/projects');
    await expectBreadcrumb(page, ['Projects']);
    await expect(breadcrumbTypes(page)).toHaveCount(0);

    await page.goto('/settings/smtp');
    await expect(breadcrumbItems(page)).toHaveCount(2);
    await expect(breadcrumbTypes(page)).toHaveCount(0);
  });

  test('lines the separators up with the first line of an item @wide', async ({ page, browser }) => {
    const id = await deploymentId(browser, PROJECT, 'prod');
    await page.goto(`/deployments/${id}`);
    const separator = page.locator('nav[data-breadcrumb="full"] li[aria-hidden="true"]').first();
    const label = page.locator('nav[data-breadcrumb="full"] [data-crumb-label]').nth(1);
    const separatorBox = await separator.boundingBox();
    const labelBox = await label.boundingBox();
    expect(Math.abs((separatorBox?.y ?? 0) - (labelBox?.y ?? 99))).toBeLessThan(2);
  });
});

test.describe('item page headers', () => {
  test('keep the title and the path line and drop the parent line', async ({ page, browser }) => {
    const id = await deploymentId(browser, PROJECT, 'prod');
    await page.goto(`/deployments/${id}`);
    await expectItemHeader(page, 'deployment', 'prod');
    await expectLocation(page, 'deployment', { root: 'Deployment path: /opt/fancy/prod' });
    await expect(itemHeader(page).getByText('Project:')).toHaveCount(0);
  });
});

test.describe('compact breadcrumb', () => {
  test.use({ viewport: { width: 360, height: 800 } });

  test('shows the parent as a back link and the current item @narrow', async ({
    page,
    browser,
  }) => {
    const id = await deploymentId(browser, PROJECT, 'prod');
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('nav[data-breadcrumb="full"]')).toBeHidden();
    const compact = compactBreadcrumb(page);
    await expect(compact.locator('li:not([aria-hidden="true"])')).toHaveText([
      `${PROJECT} Project`,
      'prod Deployment',
    ]);
    const back = compact.getByRole('link');
    await expect(back).toHaveCount(1);
    await expect(back).toHaveAttribute('href', /^\/projects\/\d+$/);
    const box = await back.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    expect(box?.width ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    await expect(compact.locator('[aria-current="page"]')).toHaveText('prod Deployment');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await back.click();
    await expect(page).toHaveURL(/\/projects\/\d+$/);
  });

  test('puts the parent and the current item above the dist check page @narrow', async ({
    page,
    browser,
  }) => {
    const id = await deploymentId(browser, PROJECT, 'prod');
    await page.goto(`/deployments/${id}/dist`);
    await expect(page.locator('nav[aria-label="Breadcrumb"]')).toBeHidden();
    await expect(compactBreadcrumb(page).locator('li:not([aria-hidden="true"])')).toHaveText([
      'prod Deployment',
      'Dist check',
    ]);
    await expect(compactBreadcrumb(page).getByRole('link')).toHaveAttribute(
      'href',
      `/deployments/${id}`,
    );
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('shows just the one item of a page without a parent @narrow', async ({ page }) => {
    await page.goto('/projects');
    const compact = compactBreadcrumb(page);
    await expect(compact.locator('li')).toHaveText(['Projects']);
    await expect(compact.getByRole('link')).toHaveCount(0);
    await expect(compact.locator('[data-crumb-type]')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test('the compact breadcrumb is hidden from the md breakpoint @wide', async ({ page }) => {
  await page.goto('/projects');
  await expect(compactBreadcrumb(page)).toBeHidden();
});
