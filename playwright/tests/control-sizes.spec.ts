import type { Locator } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { serviceId } from '../services-api';
import { TOUCH_TARGET, isWide } from '../viewport';

/*
 * The height of the shared controls. From md up a button, field or select is 37 px, a primary
 * or dialog button 41 px, a square icon button of a row 33 px (every one 3 px lower than the
 * touch sizes), and phones keep their 44 px targets.
 */

const FIELD = 37;
const PRIMARY = 41;
const ICON = 33;

async function size(control: Locator): Promise<{ width: number; height: number }> {
  const box = await control.boundingBox();
  expect(box, 'the control is on the page').not.toBeNull();
  return { width: Math.round(box?.width ?? 0), height: Math.round(box?.height ?? 0) };
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
    expect((await size(header.getByRole('link', { name: 'Access grid' }))).height).toBe(FIELD);
    expect((await size(page.getByRole('button', { name: 'Create user' }))).height).toBe(PRIMARY);
  });

  test('a dialog keeps its buttons and fields in line', async ({ page }) => {
    await page.goto('/users');
    await page.getByRole('button', { name: 'Create user' }).click();
    const dialog = page.locator('#user-create');
    await expect(dialog.locator('#un')).toBeVisible();
    expect((await size(dialog.locator('#un'))).height).toBe(FIELD);
    expect((await size(dialog.locator('#ug'))).height).toBe(FIELD);
    const cancel = await size(dialog.getByRole('button', { name: 'Cancel' }));
    const submit = await size(dialog.locator('button[type="submit"]'));
    expect(cancel.height).toBe(PRIMARY);
    expect(submit.height).toBe(PRIMARY);
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
    expect((await size(page.locator('[data-target="deploy-dialog"]'))).height).toBe(PRIMARY);
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
      expect((await size(mode)).height).toBe(ICON);
    }
    const add = panel.getByRole('button', { name: 'Add variable' });
    expect((await size(add)).height).toBe(ICON);
    await add.click();
    const row = panel.locator('#kv-list li').last();
    await expect(row.locator('input').first()).toBeVisible();
    expect((await size(row.locator('input').first())).height).toBe(FIELD);
    expect((await size(row.getByRole('button', { name: 'Remove' }))).height).toBe(ICON);
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
