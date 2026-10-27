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

/** The path lines of an item header by key: the deployment path and the target. */
export type LocationKey = 'root' | 'target';

/**
 * The header shows exactly the given path lines, in the order deployment path, target, each with
 * its text; a key that is not given must be absent. The parents of an item are not in the header:
 * the breadcrumb names them.
 */
export async function expectLocation(
  page: Page,
  type: ItemType,
  lines: Partial<Record<LocationKey, string>>,
): Promise<void> {
  const header = itemHeader(page, type);
  const keys = (['root', 'target'] as LocationKey[]).filter((key) => key in lines);
  await expect(header.locator('[data-location]')).toHaveCount(keys.length);
  for (const key of keys) {
    await expect(header.locator(`[data-location="${key}"]`)).toHaveText(lines[key] as string);
  }
  await expect(header.locator('[data-item-location]')).toHaveCount(keys.length ? 1 : 0);
  await expect(header.locator('[data-location="parents"]')).toHaveCount(0);
}

/**
 * A breadcrumb item: its label alone, or the label and the type line of a project, deployment,
 * service or environment. Sections and actions have no type.
 */
export type Crumb = string | [label: string, type: 'Project' | 'Deployment' | 'Service' | 'Environment'];

const crumbText = (crumb: Crumb): string => (typeof crumb === 'string' ? crumb : `${crumb[0]} ${crumb[1]}`);

/** The items of the breadcrumb in the top bar (from the md breakpoint), the separators left out. */
export function breadcrumbItems(page: Page): Locator {
  return page.locator('nav[data-breadcrumb="full"] li:not([aria-hidden="true"])');
}

/** The breadcrumb in the top bar shows exactly these items, the last being the current page. */
export async function expectBreadcrumb(page: Page, crumbs: Crumb[]): Promise<void> {
  await expect(breadcrumbItems(page)).toHaveText(crumbs.map(crumbText));
  await expect(breadcrumbItems(page).last()).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('nav[data-breadcrumb="full"] [aria-current="page"]')).toHaveCount(1);
}

/** The type lines of the breadcrumb in the top bar, in order. */
export function breadcrumbTypes(page: Page): Locator {
  return page.locator('nav[data-breadcrumb="full"] [data-crumb-type]');
}

/** The compact breadcrumb at the top of the page below md. */
export function compactBreadcrumb(page: Page): Locator {
  return page.locator('nav[data-breadcrumb="compact"]');
}

/** The addresses of the links of the breadcrumb in the top bar, in the order they are shown. */
export async function breadcrumbLinks(page: Page): Promise<string[]> {
  return page
    .locator('nav[data-breadcrumb="full"] a')
    .evaluateAll((links) => links.map((link) => link.getAttribute('href') ?? ''));
}

/**
 * The markup of the page without the path lines of its item header, which show the project
 * path to a caller with access to the whole deployment. Screens that print fixed, path-free copy
 * for every problem assert on this.
 */
export async function pageWithoutLocation(page: Page): Promise<string> {
  return (await page.content()).replace(/<div data-item-location[\s\S]*?<\/div>/, '');
}
