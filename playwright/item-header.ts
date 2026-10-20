import { expect, type Locator, type Page } from '@playwright/test';

/** The levels of the structure, the `data-item-type` of the header of their pages. */
export type ItemType = 'project' | 'deployment' | 'service' | 'environment';

/** The header of a project, deployment, service or environment page. */
export function itemHeader(page: Page, type?: ItemType): Locator {
  return page.locator(
    `[data-fragment="page-header"][data-item-type${type ? `="${type}"` : ''}]`,
  );
}

/** The header names the type before the item ("Deployment: prod") and keeps the name apart. */
export async function expectItemHeader(page: Page, type: ItemType, name: string): Promise<void> {
  const header = itemHeader(page, type);
  await expect(header).toHaveCount(1);
  await expect(header.locator('[data-item-name]')).toHaveText(name);
  const label = `${type.charAt(0).toUpperCase()}${type.slice(1)}:`;
  await expect(header.locator('h1')).toHaveText(`${label} ${name}`);
}

/** The location lines of an item header by key: its parents, the project path and the target. */
export type LocationKey = 'parents' | 'root' | 'target';

/**
 * The header shows exactly the given location lines, in the order parents, project path, target,
 * each with its text; a key that is not given must be absent.
 */
export async function expectLocation(
  page: Page,
  type: ItemType,
  lines: Partial<Record<LocationKey, string>>,
): Promise<void> {
  const header = itemHeader(page, type);
  const keys = (['parents', 'root', 'target'] as LocationKey[]).filter((key) => key in lines);
  await expect(header.locator('[data-location]')).toHaveCount(keys.length);
  for (const key of keys) {
    await expect(header.locator(`[data-location="${key}"]`)).toHaveText(lines[key] as string);
  }
  await expect(header.locator('[data-item-location]')).toHaveCount(keys.length ? 1 : 0);
}

/** The addresses the parent names of an item link to, in the order they are shown. */
export async function locationLinks(page: Page, type: ItemType): Promise<string[]> {
  return itemHeader(page, type)
    .locator('[data-location="parents"] a')
    .evaluateAll((links) => links.map((link) => link.getAttribute('href') ?? ''));
}

/**
 * The markup of the page without the location lines of its item header, which show the project
 * path to a caller with access to the whole deployment. Screens that print fixed, path-free copy
 * for every problem assert on this.
 */
export async function pageWithoutLocation(page: Page): Promise<string> {
  return (await page.content()).replace(/<div data-item-location[\s\S]*?<\/div>/, '');
}
