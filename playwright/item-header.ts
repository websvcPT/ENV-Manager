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
