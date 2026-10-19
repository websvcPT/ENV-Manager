import { expect, test, type Browser, type Page } from '@playwright/test';

import { deploymentId } from '../deployments-api';
import { seededEnvironmentId } from '../environments-api';
import { listProjects } from '../projects-api';
import { serviceId } from '../services-api';
import { expectImageLoaded, renderedSize } from './images';

const WIDE_BREAKPOINT = 768;

declare global {
  interface Window {
    htmx: {
      config: { mode: string; noSwap: Array<number | string> };
      ajax: (verb: string, path: string, target: string) => Promise<unknown>;
    };
    cspViolations?: string[];
  }
}

function isWide(page: Page): boolean {
  return (page.viewportSize()?.width ?? 0) >= WIDE_BREAKPOINT;
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

test.describe('layout', () => {
  test('renders the shell for a signed-in user', async ({ page }) => {
    await page.goto('/projects');
    await expect(page.locator('[data-fragment="page-header"] h1')).toBeVisible();
    await expect(page.locator('[data-fragment="projects-table"]')).toBeVisible();
    await expect(page.locator('[data-fragment="banner-area"]')).toHaveCount(1);
    await expect(page.locator('a[href="/projects"][aria-current="page"]').first()).toBeAttached();
    await expect(page.locator('header [data-js="theme-toggle"]')).toBeVisible();

    if (isWide(page)) {
      await expect(page.locator('[data-nav]')).toBeVisible();
      await expect(page.locator('[data-js="menu-open"]')).toBeHidden();
    } else {
      await expect(page.locator('[data-nav]')).toBeHidden();
      await page.locator('[data-js="menu-open"]').click();
      await expect(page.locator('#mobile-menu[open]')).toBeVisible();
      await page.locator('#mobile-menu [data-js="dialog-close"]').click();
      await expect(page.locator('#mobile-menu[open]')).toHaveCount(0);
    }
  });

  test('draws the brand mark from the served files at each size', async ({ page }) => {
    await page.goto('/projects');
    if (isWide(page)) {
      const mark = page.locator('[data-nav] img');
      await expectImageLoaded(mark);
      await expect(mark).toHaveAttribute('src', '/static/brand/env-mark-transparent.svg');
      await expect(mark).toHaveAttribute('alt', '');
      expect(await renderedSize(mark)).toEqual({ width: 36, height: 36 });
      await expect(page.locator('header img')).toBeHidden();
      await expect(page.locator('[data-nav]')).toContainText('ENV Manager');
    } else {
      const mark = page.locator('header img');
      await expectImageLoaded(mark);
      await expect(mark).toHaveAttribute('src', '/static/brand/env-mark-solid.svg');
      await expect(mark).toHaveAttribute('alt', '');
      expect(await renderedSize(mark)).toEqual({ width: 28, height: 28 });
      await expect(page.locator('header')).toContainText('ENV Manager');

      await page.locator('[data-js="menu-open"]').click();
      const menuMark = page.locator('#mobile-menu img');
      await expectImageLoaded(menuMark);
      await expect(menuMark).toHaveAttribute('src', '/static/brand/env-mark-transparent.svg');
      expect(await renderedSize(menuMark)).toEqual({ width: 36, height: 36 });
      await expect(page.locator('#mobile-menu')).toContainText('ENV Manager');
    }
  });

  test('links favicons that are served with their media type', async ({ page }) => {
    await page.goto('/projects');
    const icons = [
      ['link[rel="icon"][type="image/svg+xml"]', 'image/svg+xml'],
      ['link[rel="icon"][type="image/png"][sizes="32x32"]', 'image/png'],
      ['link[rel="apple-touch-icon"][sizes="180x180"]', 'image/png'],
    ];
    for (const [selector, mediaType] of icons) {
      const href = await page.locator(selector).getAttribute('href');
      expect(href, selector).toMatch(/^\/static\/brand\//);
      const response = await page.request.get(String(href));
      expect(response.status(), selector).toBe(200);
      expect(response.headers()['content-type'], selector).toContain(mediaType);
    }
  });

  test('configures htmx from the meta tag', async ({ page }) => {
    await page.goto('/projects');
    const config = await page.evaluate(() => window.htmx.config);
    expect(config.mode).toBe('same-origin');
    expect(config.noSwap).toContain('5xx');
  });

  test('shows a toast when an htmx request fails and keeps the content', async ({ page }) => {
    await page.goto('/projects');
    await page.evaluate(() => window.htmx.ajax('GET', '/does-not-exist', '#projects-table'));
    await expect(page.locator('[data-fragment="toast"][role="alert"]')).toBeVisible();
    await expect(page.locator('#projects-table table')).toBeVisible();
    await page.locator('[data-fragment="toast"] [data-js="dismiss"]').click();
    await expect(page.locator('[data-fragment="toast"]')).toHaveCount(0);
  });

  test('has no horizontal overflow at 360px', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    for (const path of ['/projects', '/does-not-exist']) {
      await page.goto(path);
      expect(await horizontalOverflow(page), path).toBeLessThanOrEqual(0);
    }
  });
});

test.describe('theme', () => {
  test('toggle persists across reloads', async ({ page }) => {
    await page.goto('/projects');
    const root = page.locator('html');
    const toggle = page.locator('header [data-js="theme-toggle"]');
    await expect(root).not.toHaveClass(/dark/);

    await toggle.click();
    await expect(root).toHaveClass(/dark/);
    await page.reload();
    await expect(root).toHaveClass(/dark/);

    await toggle.click();
    await expect(root).not.toHaveClass(/dark/);
    await page.reload();
    await expect(root).not.toHaveClass(/dark/);
  });
});

test.describe('sidebar', () => {
  test.use({ viewport: { width: 1280, height: 900 }, isMobile: false, hasTouch: false });

  test('collapse persists across reloads', async ({ page }) => {
    await page.goto('/projects');
    const nav = page.locator('[data-nav]');
    await expect(nav).toHaveAttribute('data-collapsed', 'false');
    const expanded = (await nav.boundingBox())?.width ?? 0;

    await nav.locator('[data-js="nav-toggle"]').click();
    await expect(nav).toHaveAttribute('data-collapsed', 'true');
    await page.reload();
    await expect(nav).toHaveAttribute('data-collapsed', 'true');
    await expect(nav.locator('[data-js="nav-toggle"]')).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(async () => (await nav.boundingBox())?.width ?? 0).toBeLessThan(expanded);

    await nav.locator('[data-js="nav-toggle"]').click();
    await page.reload();
    await expect(nav).toHaveAttribute('data-collapsed', 'false');
  });

  test('keeps the mark at full size when collapsed and hides the name', async ({ page }) => {
    await page.goto('/projects');
    const nav = page.locator('[data-nav]');
    await nav.locator('[data-js="nav-toggle"]').click();
    await expect(nav).toHaveAttribute('data-collapsed', 'true');
    const mark = nav.locator('img');
    await expectImageLoaded(mark);
    await expect(mark).toBeVisible();
    expect(await renderedSize(mark)).toEqual({ width: 36, height: 36 });
    await expect(nav.getByText('ENV Manager', { exact: true })).toBeHidden();
  });
});

test.describe('security policy', () => {
  test('has no CSP violations on the shell and the error page', async ({ page }) => {
    const consoleViolations: string[] = [];
    page.on('console', (message) => {
      if (/content security policy/i.test(message.text())) {
        consoleViolations.push(message.text());
      }
    });
    await page.addInitScript(() => {
      window.cspViolations = [];
      document.addEventListener('securitypolicyviolation', (event) => {
        window.cspViolations?.push(`${event.violatedDirective} ${event.blockedURI}`);
      });
    });

    for (const path of ['/projects', '/does-not-exist']) {
      const response = await page.goto(path);
      expect(response?.headers()['content-security-policy']).toContain("default-src 'self'");
      await page.locator('header [data-js="theme-toggle"]').click();
      await page.locator('header [data-js="theme-toggle"]').click();
      expect(await page.evaluate(() => window.cspViolations), path).toEqual([]);
    }
    expect(consoleViolations).toEqual([]);
  });
});

test.describe('error pages', () => {
  test('renders the not found page inside the shell', async ({ page }) => {
    const response = await page.goto('/does-not-exist');
    expect(response?.status()).toBe(404);
    const state = page.locator('[data-fragment="system-state"]');
    await expect(state).toBeVisible();
    await expect(state.locator('h1')).toBeVisible();
    await expect(state.locator('a[href="/projects"]').first()).toBeVisible();
    await expect(page.locator('header [data-js="theme-toggle"]')).toBeVisible();
    await expect(state.locator('img')).toHaveCount(0);
  });
});

test.describe('error pages without a session', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('draws the brand lockup on the page without the shell', async ({ page }) => {
    const response = await page.goto('/does-not-exist');
    expect(response?.status()).toBe(404);
    await expect(page.locator('[data-nav]')).toHaveCount(0);
    const lockup = page.locator('[data-fragment="system-state"] img');
    await expectImageLoaded(lockup);
    await expect(lockup).toHaveAttribute('src', '/static/brand/env-vertical-solid.svg');
    await expect(lockup).toHaveAttribute('alt', '');
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  });
});

test.describe('account menu', () => {
  test('opens from the top bar button and lists account, theme and log out', async ({ page }) => {
    await page.goto('/projects');
    const menu = page.locator('#account-menu');
    await expect(menu).toBeHidden();

    await page.locator('header [data-target="account-menu"]').click();
    await expect(menu).toBeVisible();
    await expect(menu.locator('a[href="/account"]')).toBeVisible();
    await expect(menu.locator('[data-js="theme-toggle"]')).toBeVisible();
    await expect(menu.locator('form[action="/logout"] button[type="submit"]')).toBeVisible();
    await expect(menu.locator('form[action="/logout"] input[name="csrf_token"]')).toHaveCount(1);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    const box = await menu.boundingBox();
    const viewport = page.viewportSize();
    expect(box).not.toBeNull();
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(viewport?.width ?? 0);
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
    for (const control of await menu.locator('a, button').all()) {
      expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(40);
    }

    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
  });

  test('closes on a click outside the menu', async ({ page }) => {
    await page.goto('/projects');
    await page.locator('header [data-target="account-menu"]').click();
    await expect(page.locator('#account-menu')).toBeVisible();
    await page.mouse.click(5, 5);
    await expect(page.locator('#account-menu')).toBeHidden();
  });

  test('the theme row switches the theme', async ({ page }) => {
    await page.goto('/projects');
    const root = page.locator('html');
    await expect(root).not.toHaveClass(/dark/);
    await page.locator('header [data-target="account-menu"]').click();
    await page.locator('#account-menu [data-js="theme-toggle"]').click();
    await expect(root).toHaveClass(/dark/);
    await page.locator('#account-menu [data-js="theme-toggle"]').click();
    await expect(root).not.toHaveClass(/dark/);
  });

  test('the account link leads to the account page address', async ({ page }) => {
    await page.goto('/projects');
    await page.locator('header [data-target="account-menu"]').click();
    await page.locator('#account-menu a[href="/account"]').click();
    await expect(page).toHaveURL(/\/account$/);
  });
});

test.describe('log out', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('ends the session and returns to sign in', async ({ page }) => {
    await page.goto('/login');
    await page.locator('#user').fill('viewer');
    await page.locator('#pw').fill('dev-only-password');
    await page.locator('#login-card button[type="submit"]').click();
    await expect(page).toHaveURL(/\/projects$/);

    await page.locator('header [data-target="account-menu"]').click();
    await page.locator('#account-menu form[action="/logout"] button[type="submit"]').click();
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('#user')).toBeVisible();

    await page.goto('/projects');
    await expect(page).toHaveURL(/\/login$/);
  });
});

test.describe('top bar and page titles', () => {
  async function signedInPages(page: Page, browser: Browser): Promise<string[]> {
    const fancy = (await listProjects(page.request, 'Fancy Proj. Name'))[0][0];
    const prod = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    const mysql = await serviceId(browser, 'Fancy Proj. Name', 'prod', 'mysql');
    const db = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
    return [
      '/projects',
      `/projects/${fancy}`,
      `/deployments/${prod}`,
      `/services/${mysql}`,
      `/environments/${db}`,
      `/environments/${db}/history`,
      '/audit',
      '/users',
      '/access/grid',
      '/tokens',
      '/settings/paths',
      '/account',
      '/no-such-page',
    ];
  }

  test('no page has a search box in the top bar; the projects filter stays in its section', async ({
    page,
    browser,
  }) => {
    for (const path of await signedInPages(page, browser)) {
      await page.goto(path);
      await expect(page.locator('header input[type="search"], header [role="search"]'), path).toHaveCount(0);
      await expect(page.locator('#q'), path).toHaveCount(0);
    }
    await page.goto('/projects');
    await expect(page.locator('main #ps')).toBeVisible();
  });

  test('every page title is 26px on a phone and 30px from md', async ({ page, browser }) => {
    const expected = isWide(page) ? '30px' : '26px';
    for (const path of await signedInPages(page, browser)) {
      await page.goto(path);
      const sizes = await page.locator('h1').evaluateAll((titles) =>
        titles.map((title) => getComputedStyle(title).fontSize),
      );
      expect(sizes, path).toEqual([expected]);
    }
  });

  test.describe('signed out', () => {
    test.use({ storageState: { cookies: [], origins: [] } });

    test('the sign-in card and the error page use the same title size', async ({ page }) => {
      const expected = isWide(page) ? '30px' : '26px';
      for (const path of ['/login', '/login?expired=1', '/no-such-page']) {
        await page.goto(path);
        const sizes = await page.locator('h1').evaluateAll((titles) =>
          titles.map((title) => getComputedStyle(title).fontSize),
        );
        expect(sizes, path).toEqual([expected]);
      }
    });
  });
});
