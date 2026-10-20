import type { Locator, Page } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { serviceId } from '../services-api';
import { horizontalOverflow, isWide } from '../viewport';
import { seededEditor } from './seeded-editor';

/*
 * The height of the shared controls. Every button with text and every input and select has one
 * height per layout: 37 px from md up and 44 px on a phone. What keeps another size is not a
 * button with text or an input: the square icon buttons and row actions (33 px from md up), the
 * round theme toggle and the avatar.
 */

const DESKTOP_HEIGHT = 37;
const PHONE_HEIGHT = 44;
const ICON = 33;

type Box = { x: number; y: number; width: number; height: number };

type Control = { kind: string; name: string; height: number };

/** The height every button with text, input and select has on the layout of the page. */
function controlHeight(page: Page): number {
  return isWide(page) ? DESKTOP_HEIGHT : PHONE_HEIGHT;
}

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

/**
 * Every visible button with text, link drawn as a button, text input, number or date input,
 * select and one-line text area of the page content and of the open dialogs, with the size it
 * has. Icon-only buttons, row actions, checkboxes, radios, hidden fields and the sidebar are not
 * part of it.
 */
async function controls(page: Page): Promise<Control[]> {
  return page.evaluate(() => {
    const roots = Array.from(document.querySelectorAll('main, dialog[open]'));
    const found = new Set<Element>();
    for (const root of roots) {
      for (const element of root.querySelectorAll('button, a.rounded-btn, input, select, textarea')) {
        found.add(element);
      }
    }
    const result: { kind: string; name: string; height: number }[] = [];
    for (const element of found) {
      const tag = element.tagName.toLowerCase();
      if (element.closest('[data-row-action], [data-row-menu-item]')) {
        continue;
      }
      if (tag === 'input') {
        const type = (element as HTMLInputElement).type;
        if (['hidden', 'checkbox', 'radio', 'file', 'range'].includes(type)) {
          continue;
        }
      }
      if (tag === 'textarea' && (element as HTMLTextAreaElement).rows !== 1) {
        continue;
      }
      const clone = element.cloneNode(true) as Element;
      clone.querySelectorAll('.sr-only').forEach((hidden) => hidden.remove());
      const text = (clone.textContent ?? '').trim();
      if ((tag === 'button' || tag === 'a') && text === '') {
        continue;
      }
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) {
        continue;
      }
      const label =
        text || element.getAttribute('aria-label') || element.getAttribute('name') || element.id;
      result.push({ kind: tag, name: label.slice(0, 40), height: Math.round(rect.height) });
    }
    return result;
  });
}

/** Every control of the screen has the one height of the layout, and nothing overflows. */
async function expectOneHeight(page: Page, atLeast: number): Promise<void> {
  const found = await controls(page);
  expect(found.length, 'the controls the screen is checked on').toBeGreaterThanOrEqual(atLeast);
  const expected = controlHeight(page);
  expect(
    found.filter((control) => control.height !== expected),
    `controls that are not ${expected} px`,
  ).toEqual([]);
  expect(await horizontalOverflow(page)).toBe(0);
}

/** Buttons side by side share one height, and from md up one top edge as well. */
async function expectOneLine(page: Page, buttons: Locator[]): Promise<void> {
  const boxes = await Promise.all(buttons.map(box));
  for (const found of boxes) {
    expect(found.height).toBe(controlHeight(page));
  }
  if (isWide(page)) {
    for (const found of boxes) {
      expect(found.y).toBe(boxes[0].y);
    }
  }
}

test.describe('control sizes', () => {
  test('a list page: header buttons, search field and select', async ({ page }) => {
    await page.goto('/users');
    await expectOneHeight(page, 4);
    const create = page.getByRole('button', { name: 'Create user' });
    expect((await size(page.locator('#u-q'))).height).toBe(controlHeight(page));
    expect((await size(page.locator('#u-s'))).height).toBe(controlHeight(page));
    const access = page.locator('[data-fragment="page-header"]').getByRole('link', {
      name: 'Access grid',
    });
    await expectOneLine(page, [access, create]);
  });

  test('a dialog keeps its buttons and fields at one height', async ({ page }) => {
    await page.goto('/users');
    await page.getByRole('button', { name: 'Create user' }).click();
    const dialog = page.locator('#user-create');
    await expect(dialog.locator('#un')).toBeVisible();
    await expectOneHeight(page, 4);
    expect((await size(dialog.locator('#un'))).height).toBe(controlHeight(page));
    expect((await size(dialog.locator('#ug'))).height).toBe(controlHeight(page));
    await expectOneLine(page, [
      dialog.getByRole('button', { name: 'Cancel' }),
      dialog.locator('button[type="submit"]'),
    ]);
  });

  test('the service header and a confirm dialog keep their buttons together', async ({
    page,
    browser,
  }) => {
    const id = await serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
    await page.goto(`/services/${id}`);
    await expectOneHeight(page, 4);
    await expectOneLine(page, [
      page.locator('#service-rename-open'),
      page.locator('#service-delete-open'),
      page.locator('[data-target="deploy-dialog"]'),
    ]);
    await page.locator('#service-delete-open').click();
    const dialog = page.locator('#service-delete');
    await expect(dialog.locator('button[type="submit"]')).toBeVisible();
    await expectOneHeight(page, 2);
    await expectOneLine(page, [
      dialog.getByRole('button', { name: 'Cancel' }),
      dialog.locator('button[type="submit"]'),
    ]);
  });

  test('the post-deploy settings of a service, the timeout field and its buttons', async ({
    page,
    browser,
  }) => {
    const id = await serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
    await page.goto(`/services/${id}`);
    const settings = page.locator('#postdeploy-settings');
    await expect(settings.locator('#timeout')).toBeVisible();
    expect((await size(settings.locator('#timeout'))).height).toBe(controlHeight(page));
    await expectOneLine(page, [
      settings.getByRole('button', { name: 'Test command' }),
      settings.locator('button[type="submit"]'),
    ]);
    await expectOneHeight(page, 4);
  });

  test('the notification recipients selects match the other fields', async ({ page }) => {
    await page.goto('/settings/notifications');
    const selects = page.locator('#notifications select');
    expect(await selects.count()).toBeGreaterThan(0);
    for (const select of await selects.all()) {
      expect((await size(select)).height).toBe(controlHeight(page));
    }
    expect((await size(page.locator('#wu'))).height).toBe(controlHeight(page));
    await expectOneHeight(page, 4);
  });

  test('a settings form: fields, selects and its button', async ({ page }) => {
    await page.goto('/settings/general');
    expect(
      (await size(page.locator('form button[type="submit"]').first())).height,
    ).toBe(controlHeight(page));
    await expectOneHeight(page, 3);
  });

  test('a form page: the deployment fields', async ({ page, browser }) => {
    const id = await deploymentId(browser, 'Fancy Proj. Name', 'staging');
    await page.goto(`/deployments/${id}/edit`);
    await expectOneHeight(page, 4);
  });

  test('the editor toolbar buttons and the new variable row share one height', async ({
    page,
    browser,
  }) => {
    await page.goto(`/environments/${await seededEditor(browser)}`);
    const panel = page.locator('[data-fragment="editor-panel"]');
    await expect(panel).toBeVisible();
    for (const mode of await panel.locator('[role="group"] button').all()) {
      expect((await size(mode)).height).toBe(controlHeight(page));
    }
    const add = panel.getByRole('button', { name: 'Add variable' });
    expect((await size(add)).height).toBe(controlHeight(page));
    await add.click();
    const row = panel.locator('#kv-list li').last();
    await expect(row.locator('input').first()).toBeVisible();
    const name = row.locator('input').first();
    const value = row.locator('textarea');
    expect((await size(name)).height).toBe(controlHeight(page));
    expect((await size(value)).height).toBe(controlHeight(page));
    const remove = row.getByRole('button', { name: 'Remove' });
    expect((await size(remove)).height).toBe(controlHeight(page));
    for (const reveal of await panel.getByRole('button', { name: 'Reveal' }).all()) {
      expect((await size(reveal)).height).toBe(controlHeight(page));
    }
    for (const edit of await panel.locator('button[aria-label^="Edit "]').all()) {
      expect((await size(edit)).height).toBe(controlHeight(page));
    }
    if (isWide(page)) {
      expect((await box(name)).y).toBe((await box(remove)).y);
    }
    await expectOneHeight(page, 6);
  });
});

test.describe('icon controls from md up', { tag: '@wide' }, () => {
  test('the header controls keep the size of an icon', async ({ page }) => {
    await page.goto('/users');
    expect(await size(page.locator('header [data-js="theme-toggle"]'))).toEqual({
      width: DESKTOP_HEIGHT,
      height: DESKTOP_HEIGHT,
    });
    expect((await size(page.locator('header [data-target="account-menu"]'))).height).toBe(
      DESKTOP_HEIGHT,
    );
  });

  test('the icon actions of a row', async ({ page, browser }) => {
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
});
