import { readFileSync } from 'node:fs';

import { expect, test, type Browser, type Locator, type Page } from '@playwright/test';

import { auditEntryIds } from '../audit-api';
import { stateFile, SUPERADMIN_STATE } from '../global-setup';

/*
 * The audit log. The seeded history (the development seed command adds it) holds one entry of
 * an API token, which is what the superadmin specs narrow every view to: other specs write
 * entries all the time, so an unfiltered list is never asserted on its content or order. The
 * bulk entries of the action backup.create (55, no item scope) give the second page. Entries
 * are append-only, so the exports these specs make stay in the log.
 */

const WIDE_BREAKPOINT = 768;
const TOKEN = 'api:token billing-ci';
const TOKEN_QUERY = `actor=${encodeURIComponent(TOKEN)}`;

function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

function auditRows(page: Page): Locator {
  return page.locator('[data-fragment="audit-row"]');
}

/** The toggle of a row that the current layout shows (a phone has one in the first cell). */
function toggle(row: Locator): Locator {
  return row.locator('[data-js="row-toggle"]:visible');
}

function detailsOf(page: Page, id: number): Locator {
  return page.locator(`#audit-detail-${id}`);
}

function param(page: Page, name: string): string | null {
  return new URL(page.url()).searchParams.get(name);
}

async function mainText(page: Page): Promise<string> {
  return page.locator('main').innerText();
}

/** Entry ids of a view, looked up as the superadmin so any role's spec can name a hidden entry. */
async function idsAsSuperadmin(browser: Browser, query: string): Promise<number[]> {
  const context = await browser.newContext({ storageState: SUPERADMIN_STATE });
  try {
    return await auditEntryIds(context.request, query);
  } finally {
    await context.close();
  }
}

async function tokenEntry(browser: Browser): Promise<number> {
  const [id] = await idsAsSuperadmin(browser, TOKEN_QUERY);
  expect(id).toBeGreaterThan(0);
  return id;
}

test.describe('superadmin', () => {
  test('lists the entry with every column and expands its details and version', async ({
    page,
    browser,
  }) => {
    await page.goto(`/audit?${TOKEN_QUERY}`);
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('Audit log');
    await expect(page.locator('a[href="/audit"][aria-current="page"]').first()).toBeAttached();
    await expect(page.locator('[data-fragment="page-header"]')).not.toContainText('granted');
    await expect(page.getByRole('note')).toHaveCount(0);
    await expect(page.locator('#audit-results thead th')).toHaveText([
      'Time',
      'Actor',
      'Source IP',
      'Action',
      'Object',
      'Result',
      'Details',
    ]);

    const row = auditRows(page);
    await expect(row).toHaveCount(1);
    await expect(row.locator('td').nth(0)).toContainText('2026-10-16 18:10');
    await expect(row.locator('td').nth(1)).toContainText('billing-ci');
    await expect(row.locator('td').nth(1)).toContainText('token');
    await expect(row.locator('td').nth(2)).toHaveText('203.0.113.9');
    await expect(row.locator('td').nth(3)).toHaveText('service.deploy');
    await expect(row.locator('td').nth(4)).toHaveText('api');
    await expect(row.locator('td').nth(5).locator('[data-status]')).toHaveAttribute(
      'data-status',
      'halted',
    );

    const id = await tokenEntry(browser);
    const details = detailsOf(page, id);
    await expect(details).toBeHidden();
    await expect(toggle(row)).toHaveAttribute('aria-expanded', 'false');
    await toggle(row).click();
    await expect(toggle(row)).toHaveAttribute('aria-expanded', 'true');
    await expect(details).toBeVisible();
    await expect(details).toContainText('Version: .env.api v1');
    await expect(details).toContainText('.env.api');
    await expect(details).toContainText(`Entry #${id}`);
    await expect(details.locator('[hx-ignore]')).toHaveCount(1);
    await toggle(row).click();
    await expect(details).toBeHidden();
  });

  test('filters on the server, combines them and keeps them in the URL', async ({ page }) => {
    await page.goto('/audit');
    await expect(page.locator('#f-p option').first()).toHaveText('All');

    await page.locator('#f-u').selectOption(TOKEN);
    await expect(auditRows(page)).toHaveCount(1);
    await expect.poll(() => param(page, 'actor')).toBe(TOKEN);

    await page.locator('#f-r').selectOption('halted');
    await expect(auditRows(page)).toHaveCount(1);
    await expect.poll(() => param(page, 'result')).toBe('halted');
    await expect(page.locator('#audit-export')).toHaveAttribute(
      'href',
      /^\/audit\/export\.csv\?actor=api%3Atoken\+billing-ci&result=halted$/,
    );

    await page.locator('#f-r').selectOption('failure');
    await expect(auditRows(page)).toHaveCount(0);
    await expect(page.locator('#audit-results [data-fragment="empty-state"]')).toContainText(
      'No entries match',
    );
    await expect(page.locator('[data-fragment="skeleton"]')).toBeHidden();

    await page.locator('#f-r').selectOption('');
    await expect(auditRows(page)).toHaveCount(1);
    await page.locator('#f-d1').fill('2026-10-17');
    await expect(auditRows(page)).toHaveCount(0);
    await expect.poll(() => param(page, 'from')).toBe('2026-10-17');
    await page.locator('#f-d1').fill('2026-10-16');
    await page.locator('#f-d2').fill('2026-10-16');
    await expect(auditRows(page)).toHaveCount(1);
    await expect.poll(() => param(page, 'to')).toBe('2026-10-16');
    await page.locator('#f-d2').fill('2026-10-15');
    await expect(auditRows(page)).toHaveCount(0);
  });

  test('reload and back restore the filters inside the whole page', async ({ page }) => {
    await page.goto('/audit');
    await page.locator('#f-u').selectOption(TOKEN);
    await expect.poll(() => param(page, 'actor')).toBe(TOKEN);
    await page.locator('#f-r').selectOption('halted');
    await expect.poll(() => param(page, 'result')).toBe('halted');
    await expect(auditRows(page)).toHaveCount(1);

    await page.reload();
    await expect(page.locator('#f-u')).toHaveValue(TOKEN);
    await expect(page.locator('#f-r')).toHaveValue('halted');
    await expect(auditRows(page)).toHaveCount(1);

    await page.goBack();
    await expect.poll(() => param(page, 'result')).toBeNull();
    await expect(page.locator('#f-u')).toHaveValue(TOKEN);
    await expect(page.locator('#f-r')).toHaveValue('');
    await expect(page.locator('[data-fragment="page-header"] h1')).toHaveText('Audit log');
    await expect(page.locator('#audit-filters')).toHaveCount(1);
    await expect(page.locator('main')).toHaveCount(1);
    await expect(auditRows(page)).toHaveCount(1);
    if (isWide(page)) {
      await expect(page.locator('[data-nav]')).toBeVisible();
    }
  });

  test('a second page loads with load more and keeps an expanded row', async ({ page }) => {
    await page.goto('/audit?action=backup.create');
    await expect(auditRows(page)).toHaveCount(50);
    await toggle(auditRows(page).first()).click();
    const firstId = await auditRows(page).first().getAttribute('id');
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(auditRows(page)).toHaveCount(55);
    await expect(page.locator('[data-fragment="audit-more"]')).toHaveCount(0);
    await expect(toggle(auditRows(page).first())).toHaveAttribute('aria-expanded', 'true');
    const cells = await auditRows(page).evaluateAll((rows) =>
      rows.map((row) => row.querySelectorAll(':scope > td').length),
    );
    expect(new Set(cells)).toEqual(new Set([7]));
    const ids = await auditRows(page).evaluateAll((rows) => rows.map((row) => row.id));
    expect(new Set(ids).size).toBe(55);
    expect(ids[0]).toBe(firstId);
  });

  test('shows the loading skeleton while a filter request is in flight', async ({ page }) => {
    await page.goto(`/audit?${TOKEN_QUERY}`);
    await expect(auditRows(page)).toHaveCount(1);
    await page.route('**/audit/table*', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await route.continue();
    });
    await page.locator('#f-r').selectOption('halted');
    const skeleton = page.locator('[data-fragment="skeleton"]');
    await expect(skeleton).toBeVisible();
    await expect(skeleton).toHaveAttribute('aria-busy', 'true');
    await expect(skeleton.locator('[role="status"]')).toHaveText('Loading audit log');
    await expect(auditRows(page)).toBeHidden();
    await expect(skeleton).toBeHidden();
    await expect(auditRows(page)).toBeVisible();
    await expect(auditRows(page)).toHaveCount(1);
  });

  test('opens one entry expanded and answers the same not-found for any other number', async ({
    page,
    browser,
  }) => {
    const id = await tokenEntry(browser);
    await page.goto(`/audit?entry=${id}`);
    await expect(auditRows(page)).toHaveCount(1);
    await expect(detailsOf(page, id)).toBeVisible();
    await expect(detailsOf(page, id)).toContainText(`Entry #${id}`);
    await expect(page.locator('#audit-filters')).toHaveCount(0);
    await page.getByRole('link', { name: 'Show all entries' }).click();
    await expect(page).toHaveURL(/\/audit$/);
    await expect(page.locator('#audit-filters')).toHaveCount(1);

    const missing = await page.goto('/audit?entry=987654321');
    expect(missing?.status()).toBe(404);
    await expect(page.locator('[data-fragment="audit-entry"]')).toContainText('Entry not found');
    const first = await mainText(page);
    for (const other of ['0', 'abc', '']) {
      const response = await page.goto(`/audit?entry=${other}`);
      expect(response?.status()).toBe(404);
      expect(await mainText(page)).toBe(first);
    }
  });

  test('exports the current filter as a CSV download and audits the export', async ({ page }) => {
    await page.goto(`/audit?${TOKEN_QUERY}`);
    await expect(auditRows(page)).toHaveCount(1);
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('#audit-export').click(),
    ]);
    expect(download.suggestedFilename()).toMatch(/^audit-log-\d{8}-\d{6}\.csv$/);
    const lines = readFileSync(await download.path(), 'utf8').trim().split('\r\n');
    expect(lines[0]).toBe(
      'id,time_utc,actor,actor_type,source_ip,action,object_type,object,result,version_ids,details',
    );
    expect(lines).toHaveLength(2);
    expect(lines[1]).toContain(',billing-ci,token,203.0.113.9,service.deploy,service,api,halted,');

    await page.locator('#f-r').selectOption('failure');
    await expect(page.locator('#audit-results [data-fragment="empty-state"]')).toBeVisible();
    const [empty] = await Promise.all([
      page.waitForEvent('download'),
      page.locator('#audit-export').click(),
    ]);
    expect(readFileSync(await empty.path(), 'utf8').trim().split('\r\n')).toHaveLength(1);

    await page.goto('/audit?action=audit.export');
    const recorded = page.locator('[id^="audit-detail-"]').filter({ hasText: TOKEN });
    await expect(recorded.first()).toBeAttached();
    await expect(recorded.first()).toContainText('rows:');
  });

  test('raises no content security policy violation while it is used', async ({ page }) => {
    const violations: string[] = [];
    page.on('console', (message) => {
      if (/content security policy/i.test(message.text())) {
        violations.push(message.text());
      }
    });
    await page.addInitScript(() => {
      (window as unknown as { cspViolations: string[] }).cspViolations = [];
      document.addEventListener('securitypolicyviolation', (event) => {
        (window as unknown as { cspViolations: string[] }).cspViolations.push(
          `${event.violatedDirective} ${event.blockedURI}`,
        );
      });
    });
    await page.goto('/audit?action=backup.create');
    await expect(auditRows(page)).toHaveCount(50);
    await toggle(auditRows(page).first()).click();
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(auditRows(page)).toHaveCount(55);
    await page.locator('#f-r').selectOption('failure');
    await expect(auditRows(page)).toHaveCount(5);
    await page.goto('/audit?entry=987654321');
    const found = await page.evaluate(
      () => (window as unknown as { cspViolations: string[] }).cspViolations,
    );
    expect(found).toEqual([]);
    expect(violations).toEqual([]);
  });

  test('fits the viewport and keeps a row toggle that can be touched', async ({ page }) => {
    await page.goto('/audit?action=backup.create');
    await expect(auditRows(page)).toHaveCount(50);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    if (!isWide(page)) {
      await page.setViewportSize({ width: 360, height: 800 });
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
      const box = await toggle(auditRows(page).first()).boundingBox();
      expect(box).not.toBeNull();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.width).toBeGreaterThanOrEqual(44);
      expect(box!.x + box!.width).toBeLessThanOrEqual(360);
      await expect(page.locator('#f-d2')).toBeInViewport();
      await expect(page.locator('#audit-export')).toBeInViewport();
    } else {
      await expect(page.locator('[data-nav]')).toBeVisible();
    }
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('is limited to its scope, says so and offers only values from it', async ({ page }) => {
    await page.goto('/audit?action=project.rename');
    await expect(page.locator('[data-fragment="page-header"]')).toContainText(
      'Entries for the projects, deployments and environments granted to you.',
    );
    await expect(page.getByRole('note')).toHaveText('Showing entries within your granted scope only.');
    await expect(auditRows(page)).toHaveCount(1);
    await expect(auditRows(page).locator('td').nth(4)).toHaveText('rentals');
    await expect(page.locator('#f-p option')).toHaveText(['All assigned', 'billing', 'rentals']);
    const actors = await page.locator('#f-u option').evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value),
    );
    expect(actors).toContain(TOKEN);
    expect(actors).not.toContain('cli:cli');
    expect(actors).not.toContain('system:system');
    const actions = await page.locator('#f-a option').evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value),
    );
    for (const hidden of ['backup.create', 'user.create', 'login.failure']) {
      expect(actions).not.toContain(hidden);
    }
    await expect(page.locator('a[href="/audit"]').first()).toBeAttached();
  });

  test('shows no entry without a scope, answers a hidden entry like a missing one', async ({
    page,
    browser,
  }) => {
    await page.goto('/audit?action=backup.create');
    await expect(auditRows(page)).toHaveCount(0);
    await expect(page.locator('#audit-results [data-fragment="empty-state"]')).toContainText(
      'No entries match',
    );
    const [hidden] = await idsAsSuperadmin(browser, 'action=backup.create');
    const response = await page.goto(`/audit?entry=${hidden}`);
    expect(response?.status()).toBe(404);
    const hiddenText = await mainText(page);
    await page.goto('/audit?entry=987654321');
    expect(await mainText(page)).toBe(hiddenText);

    const csv = await page.request.get('/audit/export.csv?action=backup.create');
    expect(csv.status()).toBe(200);
    expect((await csv.text()).trim().split('\r\n')).toHaveLength(1);
  });
});

test.describe('scoped admin', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('sees the entries of its file only and only its parent project by name', async ({
    page,
    browser,
  }) => {
    await page.goto('/audit?action=file_save');
    await expect(auditRows(page)).toHaveCount(1);
    await expect(auditRows(page).locator('td').nth(4)).toHaveText('.env.db');
    const projects = await page.locator('#f-p option').allTextContents();
    expect(projects[0]).toBe('All assigned');
    expect(projects).toContain('rentals');
    expect(projects).not.toContain('billing');
    await expect(page.locator('main')).not.toContainText('billing');

    // A service deploy names the service, which an environment grant does not cover, so the
    // scoped admin sees only the deploys it ran itself (the real deploy specs leave some).
    await page.goto('/audit?action=service.deploy');
    const deployActors = await auditRows(page)
      .locator('td:nth-child(2) > span:first-of-type')
      .allTextContents();
    expect(deployActors.every((actor) => actor === 'env_scoped_admin')).toBe(true);
    const id = await tokenEntry(browser);
    const response = await page.goto(`/audit?entry=${id}`);
    expect(response?.status()).toBe(404);
    await expect(page.locator('main')).not.toContainText('billing');
    await expect(page.locator('main')).toContainText('Entry not found');
  });

  test('opens an entry it made itself, which names no item of its scope, and no one else', async ({
    page,
    browser,
  }) => {
    await page.goto('/audit?action=login.success');
    await expect(auditRows(page).first()).toBeVisible();
    const actors = await auditRows(page).locator('td:nth-child(2) > span:first-of-type').allTextContents();
    expect(new Set(actors)).toEqual(new Set(['env_scoped_admin']));
    const own = await auditRows(page).first().getAttribute('id');
    const ownId = Number(own?.replace('audit-entry-', ''));
    expect(ownId).toBeGreaterThan(0);
    const opened = await page.goto(`/audit?entry=${ownId}`);
    expect(opened?.status()).toBe(200);
    await expect(auditRows(page)).toHaveCount(1);
    await expect(detailsOf(page, ownId)).toBeVisible();
    await expect(detailsOf(page, ownId)).toContainText(`Entry #${ownId}`);

    const [rootSignIn] = await idsAsSuperadmin(browser, 'action=login.success&actor=user%3Aroot');
    expect(rootSignIn).toBeGreaterThan(0);
    const hidden = await page.goto(`/audit?entry=${rootSignIn}`);
    expect(hidden?.status()).toBe(404);
    await expect(page.locator('main')).toContainText('Entry not found');
  });
});

test.describe('admin without grants', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('sees only its own entries, naming nothing else', async ({ page }) => {
    await page.goto('/audit');
    await expect(auditRows(page).first()).toBeVisible();
    await expect(page.locator('#audit-results [data-fragment="empty-state"]')).toHaveCount(0);
    const actors = await auditRows(page).locator('td:nth-child(2) > span:first-of-type').allTextContents();
    expect(actors.length).toBeGreaterThan(0);
    expect(new Set(actors)).toEqual(new Set(['ungranted_admin']));
    await expect(page.locator('#f-p option')).toHaveText(['All assigned']);
    await expect(page.locator('main')).not.toContainText('rentals');
    await expect(page.locator('main')).not.toContainText('billing');
  });

  test('reads an empty result and exports only the header when nothing matches', async ({
    page,
  }) => {
    await page.goto('/audit?action=service.deploy');
    await expect(page.locator('#audit-results [data-fragment="empty-state"]')).toContainText(
      'No entries match',
    );
    await expect(auditRows(page)).toHaveCount(0);
    const csv = await page.request.get('/audit/export.csv?action=service.deploy');
    expect((await csv.text()).trim().split('\r\n')).toHaveLength(1);
  });
});

for (const role of ['viewer', 'envScopedViewer'] as const) {
  test.describe(`${role}`, () => {
    test.use({ storageState: stateFile(role) });

    test('has no audit link and is refused every audit address', async ({ page }) => {
      await page.goto('/projects');
      await expect(page.locator('a[href="/audit"]')).toHaveCount(0);
      for (const path of ['/audit', '/audit?entry=1', '/audit/export.csv']) {
        const response = await page.goto(path);
        expect(response?.status()).toBe(403);
        await expect(page.locator('main')).toContainText('No access');
      }
      for (const path of ['/audit/table', '/audit/rows']) {
        const response = await page.request.get(path, { headers: { 'HX-Request': 'true' } });
        expect(response.status()).toBe(403);
      }
    });
  });
}
