import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Locator, Page } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { rowAction } from '../tests/row-actions';
import { expect, test } from '../tests/scratch-project';
import { horizontalOverflow, isWide, TOUCH_TARGET } from '../viewport';

/*
 * The Logs tab of the demo deployment with a usable Docker: the lines of the real containers
 * appear, the engine setting the db container prints is a chip and never in the clear, the
 * service chips and the text filter narrow the box, Paused stops the requests and a failed
 * update is a notice. `make e2e-demo` starts the demo workloads before it runs these specs, and
 * earlier specs may have restarted a container, so a container's start line can appear more than
 * once.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const TARGETS = join(DEMO_ROOT, 'stack', 'environments');
const LINES = '/logs/lines';
const POLL_MS = 2000;
const PRINTED = 'starting with engine';

/** The value the db container reads from its file on disk, which it prints when it starts. */
function engineValue(): string {
  const text = readFileSync(join(TARGETS, '.env.db'), 'utf8');
  const found = /^DEMO_DB_ENGINE=(.+)$/m.exec(text);
  expect(found, 'the demo .env.db holds DEMO_DB_ENGINE').not.toBeNull();
  return (found as RegExpExecArray)[1];
}

function box(page: Page): Locator {
  return page.locator('#log-lines');
}

function lines(page: Page): Locator {
  return box(page).locator('[data-log-line]');
}

function printedLines(page: Page): Locator {
  return lines(page).filter({ hasText: PRINTED });
}

function followButton(page: Page): Locator {
  return page.locator('#log-follow');
}

/** Count the requests for lines the page sends from now on. */
function countLineRequests(page: Page): () => number {
  let seen = 0;
  page.on('request', (request) => {
    if (request.url().includes(LINES)) {
      seen += 1;
    }
  });
  return () => seen;
}

async function open(page: Page, id: number, query = ''): Promise<void> {
  await page.goto(`/deployments/${id}/logs${query}`);
  await expect(page.locator('#log-poller[data-following]')).toBeVisible();
  await expect(lines(page).first()).toBeVisible();
}

test.beforeAll(() => {
  expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
});

test.describe('Logs tab with a usable Docker', () => {
  test('shows the lines of the containers with the value as a chip, never in the clear', async ({
    page,
    browser,
  }) => {
    const value = engineValue();
    const id = await deploymentId(browser, 'demo', 'stack');
    await open(page, id);

    await expect(printedLines(page).first()).toBeVisible();
    const printed = await printedLines(page).count();
    await expect(printedLines(page).locator('[data-log-mask]')).toHaveCount(printed);
    await expect(printedLines(page).first().locator('[data-log-mask]')).toHaveText('[masked]');
    await expect(printedLines(page).first()).toHaveAttribute('data-service', 'db');
    await expect(lines(page).filter({ hasText: 'DEMO_DB_ENGINE' }).first()).toBeVisible();
    await expect(lines(page).filter({ hasText: 'DEMO_API_TOKEN' }).first()).toHaveAttribute(
      'data-service',
      'api',
    );
    expect(await page.content()).not.toContain(value);
    expect(await page.locator('#log-box').innerText()).not.toContain(value);
    await expect(page.locator('#log-poller')).toContainText(
      /Following, new lines every 2 s · \d+ lines shown/,
    );
    await expect(page.locator('#log-poller [data-log-count]')).toHaveText(
      String(await lines(page).count()),
    );
    await expect(followButton(page)).toHaveAttribute('aria-pressed', 'true');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });

  test('the service chips and the text filter narrow the box', async ({ page, browser }) => {
    const value = engineValue();
    const id = await deploymentId(browser, 'demo', 'stack');
    await open(page, id);
    await expect(page.locator('[data-log-service]')).toHaveText(['All services', 'api', 'db']);
    await expect(box(page).locator('[data-log-line][data-service="api"]').first()).toBeVisible();

    await page.locator('[data-log-service="api"]').click();
    await expect(box(page).locator('[data-log-line][data-service="api"]').first()).toBeVisible();
    await expect(page.locator('[data-log-line]:not([data-service="api"])')).toHaveCount(0);
    await expect(printedLines(page)).toHaveCount(0);

    await page.locator('[data-log-service=""]').click();
    await expect(printedLines(page).first()).toBeVisible();

    await page.locator('#log-q').fill('engine');
    await expect(lines(page).filter({ hasNotText: /engine/i })).toHaveCount(0);
    await expect(lines(page).filter({ hasText: 'DEMO_DB_ENGINE' }).first()).toBeVisible();
    const texts = await lines(page).allInnerTexts();
    expect(texts.length).toBeGreaterThanOrEqual(2);
    expect(texts.every((text) => text.toLowerCase().includes('engine'))).toBe(true);
    await expect(printedLines(page).first().locator('[data-log-mask]')).toHaveText('[masked]');

    await page.locator('#log-q').fill(value);
    await expect(lines(page)).toHaveCount(0);
    await expect(page.locator('#log-poller [data-log-count]')).toHaveText('0');

    await page.locator('#log-q').fill('');
    await page.locator('#log-tail').selectOption('1000');
    await expect(printedLines(page).first()).toBeVisible();
    expect(await page.content()).not.toContain(`>${value}<`);
    expect(await box(page).innerText()).not.toContain(value);
  });

  test('keeps polling while following and stops its requests when paused', async ({
    page,
    browser,
  }) => {
    test.setTimeout(60_000);
    const id = await deploymentId(browser, 'demo', 'stack');
    await open(page, id);
    const requests = countLineRequests(page);

    await expect.poll(requests, { timeout: 4 * POLL_MS + 2000 }).toBeGreaterThanOrEqual(2);

    await followButton(page).click();
    await expect(followButton(page)).toHaveAttribute('aria-pressed', 'false');
    await expect(followButton(page)).toContainText('Paused');
    await expect(page.locator('#log-poller')).toContainText('Paused');
    await expect(page.locator('#log-poller[data-following]')).toHaveCount(0);
    await expect(page.locator('#log-poller')).not.toHaveAttribute('hx-get', /.+/);
    await page.waitForTimeout(500);
    const paused = requests();
    await page.waitForTimeout(3 * POLL_MS + 500);
    expect(requests()).toBe(paused);

    await followButton(page).click();
    await expect(followButton(page)).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(requests, { timeout: 3 * POLL_MS + 2000 }).toBeGreaterThan(paused + 1);
    await expect(page.locator('#log-poller[data-following]')).toBeVisible();
  });

  test('a failed update is a notice that goes away with the next one, and the box is kept', async ({
    page,
    browser,
  }) => {
    test.setTimeout(60_000);
    const id = await deploymentId(browser, 'demo', 'stack');
    await open(page, id);
    const before = await lines(page).count();

    await page.route(`**${LINES}*`, (route) => route.abort());
    await expect(page.locator('#log-poller [data-log-notice]')).toBeVisible({
      timeout: 3 * POLL_MS + 2000,
    });
    expect(await lines(page).count()).toBe(before);
    await page.unroute(`**${LINES}*`);
    await expect(page.locator('#log-poller [data-log-notice]')).toHaveCount(0, {
      timeout: 3 * POLL_MS + 2000,
    });
    await expect(page.locator('#log-poller[data-following]')).toBeVisible();
  });

  test('the Logs action of a service row opens the tab filtered to that service', async ({
    page,
    browser,
  }) => {
    const id = await deploymentId(browser, 'demo', 'stack');
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#runtime')).toHaveAttribute('aria-busy', 'false');
    const row = page.locator(
      '[data-fragment="service-summary-row"][data-service="db"]:visible, ' +
        '[data-fragment="service-summary-item"][data-service="db"]:visible',
    );
    await (await rowAction(row, 'logs')).click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${id}/logs\\?service=db$`));
    await expect(page.locator('[data-log-service="db"] input')).toBeChecked();
    await expect(page.locator('[data-log-line]:not([data-service="db"])')).toHaveCount(0);
    await expect(lines(page).first()).toBeVisible();
  });

  test('downloads the lines as a text file with the value masked', async ({ page, browser }) => {
    const value = engineValue();
    const id = await deploymentId(browser, 'demo', 'stack');
    await open(page, id, '?service=db');
    const download = page.waitForEvent('download');
    await page.locator('#log-download').click();
    const file = await download;
    expect(file.suggestedFilename()).toBe(`deployment-${id}-logs-db.txt`);
    const text = readFileSync(await file.path(), 'utf8');
    expect(text).toContain(`${PRINTED} [masked]`);
    expect(text).not.toContain(value);
    expect(text.trim().split('\n').every((line) => line.includes(' db '))).toBe(true);
  });

  test('has the layout of its width and no sideways scroll', async ({ page, browser }) => {
    const id = await deploymentId(browser, 'demo', 'stack');
    await open(page, id);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    const height = (await page.locator('[data-log-service=""]').boundingBox())?.height ?? 0;
    if (isWide(page)) {
      expect(height).toBeLessThan(TOUCH_TARGET);
    } else {
      expect(height).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }
    const logBox = await page.locator('#log-box').boundingBox();
    const width = page.viewportSize()?.width ?? 0;
    expect(logBox?.width ?? 0).toBeLessThanOrEqual(width);
  });
});
