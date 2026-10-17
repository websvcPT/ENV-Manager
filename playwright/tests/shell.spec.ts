import { expect, test, type Page } from '@playwright/test';

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
    await expect(page.locator('[data-fragment="empty-state"]')).toBeVisible();
    await expect(page.locator('[data-fragment="banner-area"]')).toHaveCount(1);
    await expect(page.locator('a[href="/projects"][aria-current="page"]').first()).toBeAttached();
    await expect(page.locator('[data-js="theme-toggle"]')).toBeVisible();

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
    await expect(page.locator('#projects-table [data-fragment="empty-state"]')).toBeVisible();
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
    const toggle = page.locator('[data-js="theme-toggle"]');
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
      await page.locator('[data-js="theme-toggle"]').click();
      await page.locator('[data-js="theme-toggle"]').click();
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
    await expect(page.locator('[data-js="theme-toggle"]')).toBeVisible();
  });
});
