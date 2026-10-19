import type { Browser, Locator, Page } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { listProjects } from '../projects-api';
import { serviceId } from '../services-api';
import { createUser, disableUser, uniqueUserName } from '../users-api';
import { horizontalOverflow, TOUCH_TARGET, usesMenus } from '../viewport';
import { rowAction, rowMenu } from './row-actions';

declare global {
  interface Window {
    cspViolations?: string[];
  }
}

function stagingWeb(browser: Browser): Promise<number> {
  return serviceId(browser, 'Fancy Proj. Name', 'staging', 'web');
}

/** The environment row of the layout in use: the table row from md up, the list item below. */
function environmentRow(page: Page, name: string): Locator {
  return page.locator('#environments [data-fragment="env-row"]:visible', { hasText: name });
}

interface Tooltip {
  display: string;
  content: string;
  left: string;
  right: string;
}

/**
 * The tooltip of an icon control is its ::after pseudo-element, which takes its text from the
 * aria-label and is displayed on hover and on keyboard focus only, so it is read from the
 * computed style.
 */
function tooltipOf(control: Locator): Promise<Tooltip> {
  return control.evaluate((element) => {
    const style = getComputedStyle(element, '::after');
    return { display: style.display, content: style.content, left: style.left, right: style.right };
  });
}

async function expectTooltip(control: Locator, shown: boolean): Promise<void> {
  await expect.poll(async () => (await tooltipOf(control)).display).toBe(shown ? 'block' : 'none');
}

async function recordCspViolations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.cspViolations = [];
    document.addEventListener('securitypolicyviolation', (event) => {
      window.cspViolations?.push(`${event.violatedDirective} ${event.blockedURI}`);
    });
  });
}

test.describe('icon buttons from md up', { tag: '@wide' }, () => {
  test('a tooltip with the accessible name shows on hover and goes away', async ({
    page,
    browser,
  }) => {
    await recordCspViolations(page);
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const row = environmentRow(page, '.env.web.tuning');
    const history = row.locator('[data-row-action="history"]');
    await expect(history).toHaveAttribute('aria-label', 'History');
    expect(await history.getAttribute('title')).toBeNull();
    await expectTooltip(history, false);
    await history.hover();
    await expectTooltip(history, true);
    expect((await tooltipOf(history)).content).toBe('"History"');
    await page.mouse.move(0, 0);
    await expectTooltip(history, false);
    expect(await page.evaluate(() => window.cspViolations)).toEqual([]);
    await expect(page.locator('[data-row-action][style], [data-row-menu-button][style]')).toHaveCount(0);
  });

  test('a tooltip shows on keyboard focus and follows the focus', async ({ page, browser }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const row = environmentRow(page, '.env.web.tuning');
    const edit = row.locator('[data-row-action="edit"]');
    const history = row.locator('[data-row-action="history"]');
    const dist = row.locator('[data-row-action="dist"]');
    await edit.focus();
    await page.keyboard.press('Tab');
    await expect(history).toBeFocused();
    await expectTooltip(history, true);
    expect((await tooltipOf(history)).content).toBe('"History"');
    await page.keyboard.press('Tab');
    await expect(dist).toBeFocused();
    await expectTooltip(dist, true);
    expect((await tooltipOf(dist)).content).toBe('"Compare to dist"');
    await expectTooltip(history, false);
    await page.keyboard.press('Tab');
    await expectTooltip(dist, false);
  });

  test('the tooltip of the last icon is anchored inside the row and adds no scroll', async ({
    page,
    browser,
  }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const row = environmentRow(page, '.env.web.tuning');
    const dist = row.locator('[data-row-action="dist"]');
    await dist.hover();
    await expectTooltip(dist, true);
    const anchored = await tooltipOf(dist);
    expect(anchored.right).toBe('0px');
    expect(Number.parseFloat(anchored.left)).toBeLessThan(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    const sizes = await page
      .locator('#environments .overflow-x-auto')
      .evaluate((element) => [element.scrollWidth, element.clientWidth]);
    expect(sizes[0]).toBeLessThanOrEqual(sizes[1]);
  });

  test('every control carries its label as accessible name and as tooltip, on every list', async ({
    page,
    browser,
  }) => {
    const fancy = (await listProjects(page.request, 'Fancy Proj. Name'))[0][0];
    const prod = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    const pages = [
      '/users',
      '/tokens',
      `/projects/${fancy}`,
      `/projects/${fancy}/access`,
      `/deployments/${prod}`,
      `/services/${await stagingWeb(browser)}`,
      '/settings/backup',
      '/settings/paths',
      '/account',
    ];
    let seen = 0;
    for (const path of pages) {
      await page.goto(path);
      const result = await page.evaluate(() => {
        const problems: string[] = [];
        let count = 0;
        const rows = new Map<Element, number>();
        for (const control of document.querySelectorAll('[data-row-action]')) {
          const label = control.getAttribute('aria-label');
          const row = control.closest('tr, li');
          if (label === null) {
            if (row) rows.set(row, (rows.get(row) ?? 0) + 1);
            continue;
          }
          count += 1;
          const tip = getComputedStyle(control, '::after').content;
          if (tip !== `"${label}"`) {
            problems.push(`${control.id}: ${label} / ${tip}`);
          }
          if (control.hasAttribute('title')) {
            problems.push(`${control.id} has a title`);
          }
        }
        for (const [row, texts] of rows) {
          if (texts > 1) {
            problems.push(`row ${row.id || row.className} has ${texts} text actions`);
          }
        }
        return { problems, count };
      });
      expect(result.problems, path).toEqual([]);
      seen += result.count;
    }
    expect(seen).toBeGreaterThan(10);
  });
});

test.describe('3-dot menu below md', { tag: '@narrow' }, () => {
  test('lists icon and text, is a touch target and opens, closes and returns focus', async ({
    page,
    browser,
  }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const row = environmentRow(page, '.env.web.tuning');
    const more = row.locator('[data-row-menu-button]');
    const menu = rowMenu(page);
    await expect(more).toHaveAttribute('aria-label', 'More actions for .env.web.tuning');
    await expect(more).toHaveAttribute('aria-haspopup', 'dialog');
    expect((await more.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    expect((await more.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    await expect(row.locator('[data-row-action]:visible')).toHaveCount(0);
    await expect(menu).toBeHidden();
    await expect(page.locator('dialog[data-row-menu]')).toHaveCount(1);
    await expect(row.locator('[data-row-menu-item]')).toHaveCount(0);

    await more.click();
    await expect(menu).toBeVisible();
    await expect(menu).toHaveAttribute('aria-label', 'Actions for .env.web.tuning');
    await expect(menu.locator('[data-row-menu-title]')).toHaveText('.env.web.tuning');
    const items = menu.locator('[data-row-menu-item]');
    await expect(items).toHaveCount(3);
    await expect(items).toHaveText(['Edit', 'History', 'Compare to dist']);
    for (const item of await items.all()) {
      await expect(item.locator('svg')).toHaveCount(1);
      expect((await item.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    await expect(more).toBeFocused();
  });

  test('the menu is built for the row that opened it', async ({ page, browser }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const menu = rowMenu(page);
    const tuning = environmentRow(page, '.env.web.tuning').locator('[data-row-menu-button]');
    const pem = environmentRow(page, 'web-ca.pem').locator('[data-row-menu-button]');
    await tuning.click();
    await expect(menu.locator('[data-row-menu-title]')).toHaveText('.env.web.tuning');
    await expect(menu.locator('[data-row-menu-item]')).toHaveCount(3);
    await page.keyboard.press('Escape');
    await pem.click();
    await expect(menu.locator('[data-row-menu-title]')).toHaveText('web-ca.pem');
    await expect(menu.locator('[data-row-menu-item="history"]')).toHaveAttribute(
      'href',
      /^\/environments\/\d+\/history$/,
    );
    await page.keyboard.press('Escape');
    await expect(pem).toBeFocused();
  });

  test('works with the keyboard, closes on a click outside and an item navigates', async ({
    page,
    browser,
  }) => {
    await page.goto(`/services/${await stagingWeb(browser)}`);
    const row = environmentRow(page, '.env.web.tuning');
    const more = row.locator('[data-row-menu-button]');
    const menu = rowMenu(page);
    const items = menu.locator('[data-row-menu-item]');

    await more.focus();
    await page.keyboard.press('Enter');
    await expect(menu).toBeVisible();
    await expect(items.nth(0)).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(items.nth(1)).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(items.nth(0)).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    await expect(more).toBeFocused();

    await more.click();
    await expect(menu).toBeVisible();
    await page.mouse.click(8, 8);
    await expect(menu).toBeHidden();
    await expect(more).toBeFocused();

    await more.click();
    const history = menu.locator('[data-row-menu-item="history"]');
    await expect(history).toHaveAttribute('href', /^\/environments\/\d+\/history$/);
    await history.click();
    await expect(page).toHaveURL(/\/environments\/\d+\/history$/);
  });

  test('a single action shows its text without a menu', async ({ page }) => {
    await page.goto('/tokens');
    const revoke = page.locator('#tokens-table [data-row-action="revoke"]').first();
    await expect(revoke).toBeVisible();
    await expect(revoke).toContainText('Revoke');
    expect((await revoke.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    await expect(page.locator('#tokens-table [data-row-menu-button]')).toHaveCount(0);
    expect((await tooltipOf(revoke)).display).toBe('none');
  });

  test('a primary action keeps its text on desktop and joins the menu here', async ({ page }) => {
    await page.goto('/users');
    const row = page.locator('[data-fragment="user-row"]').first();
    await expect(row.locator('[data-row-menu-button]')).toBeVisible();
    await row.locator('[data-row-menu-button]').click();
    await expect(rowMenu(page).locator('[data-row-menu-item="edit"]')).toContainText('Edit');
    await expect(rowMenu(page).locator('[data-row-menu-item="edit"] svg')).toBeVisible();
  });
});

test.describe('destructive actions keep their confirm dialog', () => {
  test('Disable opens the confirm dialog from the icon or the menu and Escape changes nothing', async ({
    page,
    request,
  }, testInfo) => {
    const user = await createUser(request, uniqueUserName(testInfo.project.name), 'viewer');
    try {
      await page.goto('/users');
      const row = page.locator('[data-fragment="user-row"]', {
        has: page.locator('th[scope="row"] a', { hasText: new RegExp(`^${user.name}$`) }),
      });
      const trigger = usesMenus(page)
        ? row.locator('[data-row-menu-button]')
        : row.locator('[data-row-action="disable"]');
      await (await rowAction(row, 'disable')).click();
      const dialog = page.locator('#user-action[open]');
      await expect(dialog.getByRole('heading', { name: `Disable ${user.name}` })).toBeVisible();
      await expect(page.locator('dialog[data-row-menu][open]')).toHaveCount(0);
      await expect(dialog.getByRole('button', { name: 'Disable user' })).toBeVisible();

      await page.keyboard.press('Escape');
      await expect(page.locator('#user-action[open]')).toHaveCount(0);
      await expect(trigger).toBeFocused();
      await expect(row.locator('td').nth(2)).toHaveText('Active');
    } finally {
      await disableUser(request, user.id);
    }
  });
});
