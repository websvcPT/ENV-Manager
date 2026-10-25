import type { Locator, Page } from '@playwright/test';

import { expect, test } from '@playwright/test';

import { listRegistries, removeRegistry } from '../settings-api';
import { actionControls, rowAction } from '../tests/row-actions';
import { horizontalOverflow } from '../viewport';

/*
 * The Registries tab against a usable Docker and a real registry with a login, which
 * `make e2e-demo` starts on the host and names in E2E_DEMO_REGISTRY (host and port),
 * E2E_DEMO_REGISTRY_USER and E2E_DEMO_REGISTRY_PASSWORD. The superadmin adds the login, tests it,
 * replaces the token with a wrong one, sees the registry's refusal and removes the login. Neither
 * token is ever on the page. The login is shared application data, so it is removed before and
 * after the test as well.
 */

const HOST = process.env.E2E_DEMO_REGISTRY ?? '';
const USERNAME = process.env.E2E_DEMO_REGISTRY_USER ?? '';
const PASSWORD = process.env.E2E_DEMO_REGISTRY_PASSWORD ?? '';
const WRONG_TOKEN = 'e2e-wrong-registry-token';
// Docker gives up on a login after 30 seconds, so a result is never later than this.
const RESULT_TIMEOUT = 45_000;

async function removeLogin(page: Page): Promise<void> {
  for (const [id, listed] of await listRegistries(page.request)) {
    if (listed === HOST) {
      await removeRegistry(page.request, id);
    }
  }
}

function registryRow(page: Page): Locator {
  return page.locator('#registries-table tr[data-fragment="registry-row"]', { hasText: HOST });
}

async function expectNoToken(page: Page): Promise<void> {
  const content = await page.content();
  expect(content).not.toContain(PASSWORD);
  expect(content).not.toContain(WRONG_TOKEN);
}

/** Press Test login on the row and give the content of the result dialog once it has arrived. */
async function testLogin(page: Page): Promise<Locator> {
  await (await rowAction(registryRow(page), 'test')).click();
  const result = page.locator('#registry-test [data-fragment="registry-test"][data-result]');
  await expect(result).toBeVisible({ timeout: RESULT_TIMEOUT });
  return result;
}

async function closeResult(page: Page, result: Locator): Promise<void> {
  await result.locator('button', { hasText: /^Close$/ }).click();
  await expect(page.locator('#registry-test')).not.toHaveAttribute('open', '');
}

test.describe('registry logins against a local registry', () => {
  test.beforeEach(async ({ page }) => {
    expect(HOST, 'E2E_DEMO_REGISTRY must name the local registry').not.toBe('');
    expect(USERNAME, 'E2E_DEMO_REGISTRY_USER must name its login').not.toBe('');
    expect(PASSWORD, 'E2E_DEMO_REGISTRY_PASSWORD must hold its password').not.toBe('');
    await removeLogin(page);
  });

  test.afterEach(async ({ page }) => {
    await removeLogin(page);
  });

  test('adds a login, tests it, refuses a wrong token and removes it', async ({ page }) => {
    await page.goto('/settings/registries');
    await page.locator('[data-target="registry-add"]').first().click();
    const add = page.locator('#registry-add');
    await add.locator('#ra-host').fill(HOST);
    await add.locator('#ra-user').fill(USERNAME);
    await add.locator('#ra-token').fill(PASSWORD);
    await add.locator('button[type="submit"]').click();
    await expect(add).not.toHaveAttribute('open', '');
    const row = registryRow(page);
    await expect(row).toBeVisible();
    await expect(row.locator('th')).toHaveText(HOST);
    await expect(page.locator('#registries-docker-note')).toHaveCount(0);
    await expect(actionControls(row, 'test')).toHaveCount(1);
    await expectNoToken(page);

    let result = await testLogin(page);
    await expect(result).toHaveAttribute('data-result', 'ok');
    await expect(result).toContainText('Docker signed in to the registry with the stored login.');
    await expect(result).toContainText(`${HOST} as ${USERNAME}`);
    await expectNoToken(page);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await closeResult(page, result);

    await (await rowAction(row, 'edit')).click();
    const edit = page.locator('#registry-edit');
    await expect(edit.locator('#re-host')).toHaveValue(HOST);
    await edit.locator('#re-token').fill(WRONG_TOKEN);
    await edit.locator('button[type="submit"]').click();
    await expect(edit).not.toHaveAttribute('open', '');
    await expectNoToken(page);

    result = await testLogin(page);
    await expect(result).toHaveAttribute('data-result', 'failed');
    await expect(result).toContainText('The registry did not accept the stored login.');
    await expect(result).toContainText('401 Unauthorized');
    await expectNoToken(page);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await closeResult(page, result);

    await (await rowAction(row, 'remove')).click();
    const remove = page.locator('#registry-remove');
    await expect(remove.locator('h2')).toHaveText(`Remove ${HOST}`);
    await remove.locator('button[type="submit"]').click();
    await expect(remove).not.toHaveAttribute('open', '');
    await expect(row).toHaveCount(0);
    expect((await listRegistries(page.request)).map(([, listed]) => listed)).not.toContain(HOST);
  });
});
