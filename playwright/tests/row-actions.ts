import type { Locator, Page } from '@playwright/test';

import { expect } from '@playwright/test';

const MD_BREAKPOINT = 768;

/** Whether the page collapses row actions into a 3-dot menu (below the md breakpoint). */
export function usesMenus(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) < MD_BREAKPOINT;
}

/** The shared row menu of the shell, filled when a row's 3-dot button opens it. */
export function rowMenu(page: Page): Locator {
  return page.locator('#row-menu');
}

/**
 * The control of a row action, ready to click at the current width: the icon button or link
 * from md up, and on a phone the item of the row menu, which this opens first from the 3-dot
 * button of the row. A row with a single action shows one control at every width. Close an
 * opened menu with Escape when the spec goes on without using the item.
 */
export async function rowAction(scope: Locator, key: string): Promise<Locator> {
  const direct = scope.locator(`[data-row-action="${key}"]:visible`).first();
  if ((await direct.count()) > 0) {
    return direct;
  }
  await scope.locator('[data-row-menu-button]:visible').first().click();
  const item = rowMenu(scope.page()).locator(`[data-row-menu-item="${key}"]`);
  await expect(item).toBeVisible();
  return item;
}

/**
 * Every control of an action in a row or panel, visible or not (a row draws its controls at
 * every width and hides them below md). Use it to read an attribute such as the address of a
 * link, or to assert that an action is absent whatever the width.
 */
export function actionControls(scope: Locator, key: string): Locator {
  return scope.locator(`[data-row-action="${key}"]`);
}
