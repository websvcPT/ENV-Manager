import { expect, test, type Page } from '@playwright/test';

import { totpCode } from './totp';

// Development dataset values (development and testing only).
const PASSWORD = 'dev-only-password';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXP';
const SESSION_COOKIE = 'envmanager_session';

test.use({ storageState: { cookies: [], origins: [] } });

async function signIn(page: Page, name: string, password: string): Promise<void> {
  await page.locator('#user').fill(name);
  await page.locator('#pw').fill(password);
  await page.locator('#login-card button[type="submit"]').click();
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

test.describe('sign in page', () => {
  test('shows the card with both fields and no message', async ({ page }) => {
    await page.goto('/login');
    const card = page.locator('[data-fragment="login-card"]');
    await expect(card.locator('h1')).toBeVisible();
    await expect(card.locator('#user')).toBeEnabled();
    await expect(card.locator('#pw')).toHaveAttribute('type', 'password');
    await expect(card.locator('button[type="submit"]')).toBeEnabled();
    await expect(card.locator('a[href*="recover"], a[href*="forgot"]')).toHaveCount(0);
    await expect(page.locator('[data-fragment="login-message"]')).toHaveCount(0);
    await expect(page.locator('[data-js="theme-toggle"]')).toBeVisible();
  });

  test('has no horizontal overflow at 360px in every state', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    for (const path of ['/login', '/login?expired=1', '/session-expired']) {
      await page.goto(path);
      expect(await horizontalOverflow(page), path).toBeLessThanOrEqual(0);
    }
  });

  test('signing in lands on the projects page', async ({ page }) => {
    await page.goto('/login');
    await signIn(page, 'root', PASSWORD);
    await expect(page).toHaveURL(/\/projects$/);
    await expect(page.locator('[data-fragment="page-header"] h1')).toBeVisible();
    await page.goto('/login');
    await expect(page).toHaveURL(/\/projects$/);
  });

  test('wrong credentials swap in a generic message and keep the name', async ({ page }) => {
    await page.goto('/login');
    await signIn(page, 'root', 'not-the-password');
    const message = page.locator('[data-fragment="login-message"]');
    await expect(message).toHaveAttribute('role', 'alert');
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('#user')).toHaveValue('root');
    await expect(page.locator('#pw')).toHaveValue('');
    const wrongPassword = await message.innerText();

    await signIn(page, 'nobody-by-that-name', 'not-the-password');
    await expect(page.locator('#user')).toHaveValue('nobody-by-that-name');
    expect(await page.locator('[data-fragment="login-message"]').innerText()).toBe(wrongPassword);
    await expect(page.locator('[data-fragment="login-card"]')).toHaveCount(1);
  });

  test('repeated failures lock the form and show the time left', async ({ page }, testInfo) => {
    const name = `lockout-${testInfo.project.name}-${Date.now()}`;
    await page.goto('/login');
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const answered = page.waitForResponse(
        (response) => response.request().method() === 'POST' && response.url().endsWith('/login'),
      );
      await signIn(page, name, 'not-the-password');
      await answered;
    }
    const message = page.locator('[data-fragment="login-message"]');
    await expect(message).toHaveAttribute('role', 'alert');
    await expect(message).toContainText(/Try again in \d+:\d{2}\./);
    await expect(page.locator('#user')).toBeDisabled();
    await expect(page.locator('#pw')).toBeDisabled();
    await expect(page.locator('#login-card button[type="submit"]')).toBeDisabled();
  });

  test('the session expired state is a status message', async ({ page }) => {
    await page.goto('/login?expired=1');
    const message = page.locator('[data-fragment="login-message"]');
    await expect(message).toHaveAttribute('role', 'status');
    await expect(page.locator('#user')).toBeEnabled();
  });
});

test.describe('second factor step', () => {
  test('asks for a code, reports a wrong one and signs in with the right one', async ({ page }) => {
    await page.goto('/login');
    await signIn(page, 'totp_user', PASSWORD);

    const code = page.locator('#code');
    await expect(code).toBeVisible();
    await expect(page.locator('#user')).toHaveCount(0);
    await expect(page.locator('a[href="/login"]')).toBeVisible();

    await code.fill('000000');
    await page.locator('#login-card button[type="submit"]').click();
    await expect(page.locator('#code-e')).toBeVisible();
    await expect(page.locator('#code')).toHaveAttribute('aria-invalid', 'true');

    await page.locator('#code').fill(totpCode(TOTP_SECRET));
    await page.locator('#login-card button[type="submit"]').click();
    await expect(page).toHaveURL(/\/projects$/);
  });

  test('back to sign in returns to the credentials form', async ({ page }) => {
    await page.goto('/login');
    await signIn(page, 'totp_user', PASSWORD);
    await page.locator('#login-card a[href="/login"]').click();
    await expect(page.locator('#user')).toBeVisible();
  });
});

test.describe('session expiry', () => {
  test('a page request without a session goes to sign in', async ({ page }) => {
    await page.goto('/projects');
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('[data-fragment="login-message"]')).toHaveCount(0);
  });

  test('a page request with a dead session shows the expired state', async ({ page, context }) => {
    const baseURL = String(test.info().project.use.baseURL);
    await context.addCookies([{ name: SESSION_COOKIE, value: 'dead-session', url: baseURL }]);
    await page.goto('/projects');
    await expect(page).toHaveURL(/\/login\?expired=1$/);
    await expect(page.locator('[data-fragment="login-message"][role="status"]')).toBeVisible();
  });

  test('an htmx request from an open page is redirected', async ({ page, context }) => {
    await page.goto('/login');
    await signIn(page, 'root', PASSWORD);
    await expect(page).toHaveURL(/\/projects$/);

    await context.clearCookies();
    await page.evaluate(() => window.htmx.ajax('GET', '/projects', '#projects-table'));
    await expect(page).toHaveURL(/\/login$/);
  });

  test('an htmx request with a dead session goes to the expired page', async ({ page, context }) => {
    await page.goto('/login');
    await signIn(page, 'root', PASSWORD);
    await expect(page).toHaveURL(/\/projects$/);

    const baseURL = String(test.info().project.use.baseURL);
    await context.addCookies([{ name: SESSION_COOKIE, value: 'dead-session', url: baseURL }]);
    await page.evaluate(() => window.htmx.ajax('GET', '/projects', '#projects-table'));
    await expect(page).toHaveURL(/\/session-expired$/);
    const state = page.locator('[data-fragment="session-expired"]');
    await expect(state.locator('h1')).toBeVisible();
    await state.locator('a[href="/login"]').click();
    await expect(page.locator('#user')).toBeVisible();
  });
});

test.describe('security policy', () => {
  test('has no CSP violations on the sign in states', async ({ page }) => {
    await page.addInitScript(() => {
      window.cspViolations = [];
      document.addEventListener('securitypolicyviolation', (event) => {
        window.cspViolations?.push(`${event.violatedDirective} ${event.blockedURI}`);
      });
    });
    for (const path of ['/login', '/login?expired=1', '/session-expired']) {
      const response = await page.goto(path);
      expect(response?.headers()['content-security-policy']).toContain("default-src 'self'");
      const toggle = page.locator('[data-js="theme-toggle"]');
      await expect(toggle).toHaveCount(path === '/session-expired' ? 0 : 1);
      if (path !== '/session-expired') {
        await toggle.click();
        await toggle.click();
      }
      expect(await page.evaluate(() => window.cspViolations), path).toEqual([]);
    }
  });
});
