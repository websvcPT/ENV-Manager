import type { Locator } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { serviceId } from '../services-api';
import { TOUCH_TARGET, isWide } from '../viewport';

/*
 * The height of the shared controls. From md up every button (primary, secondary, danger and
 * dialog buttons alike), field and select is 37 px and a square icon button of a row is 33 px;
 * phones keep their 44 px targets.
 */

const FIELD = 37;
const ICON = 33;

type Box = { x: number; y: number; width: number; height: number };

async function box(control: Locator): Promise<Box> {
  const found = await control.boundingBox();
  expect(found, 'the control is on the page').not.toBeNull();
  return {
    x: Math.round(found?.x ?? 0),
    y: Math.round(found?.y ?? 0),
    width: Math.round(found?.width ?? 0),
    height: Math.round(found?.height ?? 0),
  };
}

async function size(control: Locator): Promise<{ width: number; height: number }> {
  const { width, height } = await box(control);
  return { width, height };
}

/** The controls of a row share one height and one top edge, so the row reads as a line. */
async function expectOneLine(controls: Locator[]): Promise<void> {
  const boxes = await Promise.all(controls.map(box));
  for (const found of boxes) {
    expect(found.height).toBe(FIELD);
    expect(found.y).toBe(boxes[0].y);
  }
}

test.describe('control sizes from md up', { tag: '@wide' }, () => {
  test('the header controls, buttons and fields of a list page', async ({ page }) => {
    await page.goto('/users');
    expect(await size(page.locator('header [data-js="theme-toggle"]'))).toEqual({
      width: FIELD,
      height: FIELD,
    });
    expect((await size(page.locator('header [data-target="account-menu"]'))).height).toBe(FIELD);
    expect((await size(page.locator('#u-q'))).height).toBe(FIELD);
    expect((await size(page.locator('#u-s'))).height).toBe(FIELD);
    const header = page.locator('[data-fragment="page-header"]');
    const access = header.getByRole('link', { name: 'Access grid' });
    const create = page.getByRole('button', { name: 'Create user' });
    expect((await size(access)).height).toBe(FIELD);
    expect((await size(create)).height).toBe(FIELD);
    await expectOneLine([access, create]);
  });

  test('a dialog keeps its buttons and fields in line', async ({ page }) => {
    await page.goto('/users');
    await page.getByRole('button', { name: 'Create user' }).click();
    const dialog = page.locator('#user-create');
    await expect(dialog.locator('#un')).toBeVisible();
    expect((await size(dialog.locator('#un'))).height).toBe(FIELD);
    expect((await size(dialog.locator('#ug'))).height).toBe(FIELD);
    await expectOneLine([
      dialog.getByRole('button', { name: 'Cancel' }),
      dialog.locator('button[type="submit"]'),
    ]);
  });

  test('the service header and a confirm dialog keep their buttons in one line', async ({
    page,
    browser,
  }) => {
    const id = await serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
    await page.goto(`/services/${id}`);
    await expectOneLine([
      page.locator('#service-rename-open'),
      page.locator('#service-delete-open'),
      page.locator('[data-target="deploy-dialog"]'),
    ]);
    await page.locator('#service-delete-open').click();
    const dialog = page.locator('#service-delete');
    await expect(dialog.locator('button[type="submit"]')).toBeVisible();
    await expectOneLine([
      dialog.getByRole('button', { name: 'Cancel' }),
      dialog.locator('button[type="submit"]'),
    ]);
  });

  test('a settings form button and the icon actions of a row', async ({ page, browser }) => {
    await page.goto('/settings/general');
    expect(
      (await size(page.locator('form button[type="submit"]').first())).height,
    ).toBe(FIELD);

    const id = await serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
    await page.goto(`/services/${id}`);
    const row = page.locator('#environments tr[data-env=".env.web.tuning"]');
    for (const key of ['edit', 'history', 'settings', 'delete']) {
      expect(await size(row.locator(`[data-row-action="${key}"]`)), key).toEqual({
        width: ICON,
        height: ICON,
      });
    }
  });

  test('the editor toolbar buttons and the new variable row share one height', async ({
    page,
    browser,
  }) => {
    const service = await serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
    await page.goto(`/services/${service}`);
    await page.locator('#environments tr[data-env=".env.web.tuning"] th a').click();
    const panel = page.locator('[data-fragment="editor-panel"]');
    await expect(panel).toBeVisible();
    for (const mode of await panel.locator('[role="group"] button').all()) {
      expect((await size(mode)).height).toBe(FIELD);
    }
    const add = panel.getByRole('button', { name: 'Add variable' });
    expect((await size(add)).height).toBe(FIELD);
    await add.click();
    const row = panel.locator('#kv-list li').last();
    await expect(row.locator('input').first()).toBeVisible();
    expect((await size(row.locator('input').first())).height).toBe(FIELD);
    expect((await size(row.getByRole('button', { name: 'Remove' }))).height).toBe(FIELD);
  });
});

test.describe('control sizes on a phone', { tag: '@narrow' }, () => {
  test('the main buttons and fields stay at their touch size', async ({ page }) => {
    expect(isWide(page)).toBe(false);
    await page.goto('/users');
    expect((await size(page.getByRole('button', { name: 'Create user' }))).height).toBeGreaterThanOrEqual(
      TOUCH_TARGET,
    );
    expect((await size(page.locator('#u-q'))).height).toBeGreaterThanOrEqual(TOUCH_TARGET);
    expect((await size(page.locator('#u-s'))).height).toBeGreaterThanOrEqual(TOUCH_TARGET);
  });
});
