import type { Page } from '@playwright/test';

/** The width from which the layout is the desktop one: the `md` breakpoint of the theme. */
export const WIDE_BREAKPOINT = 768;

/** The smallest height and width of a control that a finger has to hit on a phone. */
export const TOUCH_TARGET = 44;

/** Whether the page is at least as wide as the `md` breakpoint. */
export function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

/** Whether the page collapses row actions into a 3-dot menu (below the `md` breakpoint). */
export function usesMenus(page: Page): boolean {
  return !isWide(page);
}

/** How far the document is wider than the window; above zero the page scrolls sideways. */
export async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}
