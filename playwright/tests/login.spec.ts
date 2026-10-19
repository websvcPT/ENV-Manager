import { expect, test, type Page } from '@playwright/test';

import { expectImageLoaded, renderedSize } from './images';
import { totpCode } from './totp';

// Development dataset values (development and testing only).
const PASSWORD = 'dev-only-password';
const TOTP_SECRET = 'JBSWY3DPEHPK3PXP';
const SESSION_COOKIE = 'envmanager_session';
const PENDING_COOKIE = 'envmanager_pending_login';

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

  test('draws the brand lockup above the product name', async ({ page }) => {
    for (const path of ['/login', '/login?expired=1']) {
      await page.goto(path);
      const card = page.locator('[data-fragment="login-card"]');
      const lockup = card.locator('img');
      await expectImageLoaded(lockup);
      await expect(lockup).toHaveAttribute('src', '/static/brand/env-vertical-solid.svg');
      await expect(lockup).toHaveAttribute('alt', '');
      expect((await renderedSize(lockup)).height, path).toBe(96);
      const lockupBox = await lockup.boundingBox();
      const headingBox = await card.locator('h1').boundingBox();
      expect(lockupBox?.y ?? 0, path).toBeLessThan(headingBox?.y ?? 0);
    }
    await page.goto('/login');
    await expect(page.locator('[data-fragment="login-card"] h1')).toHaveText('ENV Manager');
  });

  test('has no horizontal overflow at 360px in every state', async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    for (const path of ['/login', '/login?expired=1']) {
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

// Each project signs in with its own second factor user, so the two projects running in
// parallel never present the same authenticator code (an accepted code cannot be reused).
function totpUser(projectName: string): string {
  return projectName === 'phone' ? 'totp_user_alt' : 'totp_user';
}

test.describe('second factor step', () => {
  test('asks for a code, reports a wrong one and signs in with the right one', async ({
    page,
  }, testInfo) => {
    await page.goto('/login');
    await signIn(page, totpUser(testInfo.project.name), PASSWORD);

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

  test('an empty code asks for it again', async ({ page }, testInfo) => {
    await page.goto('/login');
    await signIn(page, totpUser(testInfo.project.name), PASSWORD);
    await expect(page.locator('#code')).toBeVisible();
    await page.locator('#login-card button[type="submit"]').click();
    await expect(page.locator('#code-e')).toContainText('6-digit');
    await expect(page.locator('#code')).toBeVisible();
  });

  test('the code step carries no credentials and keeps the pending login in a cookie', async ({
    page,
    context,
  }, testInfo) => {
    await page.goto('/login');
    await signIn(page, totpUser(testInfo.project.name), PASSWORD);
    await expect(page.locator('#code')).toBeVisible();

    await expect(page.locator('#login-card input[type="hidden"][name="identifier"]')).toHaveCount(0);
    await expect(page.locator('#login-card input[type="hidden"][name="password"]')).toHaveCount(0);
    expect(await page.locator('#login-card').innerHTML()).not.toContain(PASSWORD);

    const pending = (await context.cookies()).find((cookie) => cookie.name === PENDING_COOKIE);
    expect(pending?.httpOnly).toBe(true);
    expect(pending?.sameSite).toBe('Strict');
    expect(pending?.path).toBe('/login');
  });

  test('a lost pending login returns to the password form with a message', async ({
    page,
    context,
  }, testInfo) => {
    await page.goto('/login');
    await signIn(page, totpUser(testInfo.project.name), PASSWORD);
    await expect(page.locator('#code')).toBeVisible();

    await context.clearCookies({ name: PENDING_COOKIE });
    await page.locator('#code').fill(totpCode(TOTP_SECRET));
    await page.locator('#login-card button[type="submit"]').click();

    const message = page.locator('[data-fragment="login-message"]');
    await expect(message).toHaveAttribute('role', 'status');
    await expect(message).toContainText('Verification expired');
    await expect(page.locator('#user')).toBeEnabled();
    await expect(page.locator('#code')).toHaveCount(0);
  });

  test('back to sign in returns to the credentials form', async ({ page }, testInfo) => {
    await page.goto('/login');
    await signIn(page, totpUser(testInfo.project.name), PASSWORD);
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

  // The request is started without awaiting it: the redirect navigates the page away, which
  // would destroy the context of an awaited evaluate.
  test('an htmx request from an open page without a session goes to sign in', async ({
    page,
    context,
  }) => {
    await page.goto('/login');
    await signIn(page, 'root', PASSWORD);
    await expect(page).toHaveURL(/\/projects$/);

    await context.clearCookies();
    await page.evaluate(() => {
      void window.htmx.ajax('GET', '/projects', '#projects-table');
    });
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.locator('[data-fragment="login-message"]')).toHaveCount(0);
  });

  test('an htmx request with a dead session goes to sign in in the expired state', async ({
    page,
    context,
  }) => {
    await page.goto('/login');
    await signIn(page, 'root', PASSWORD);
    await expect(page).toHaveURL(/\/projects$/);

    const baseURL = String(test.info().project.use.baseURL);
    await context.addCookies([{ name: SESSION_COOKIE, value: 'dead-session', url: baseURL }]);
    await page.evaluate(() => {
      void window.htmx.ajax('GET', '/projects', '#projects-table');
    });
    await expect(page).toHaveURL(/\/login\?expired=1$/);
    const message = page.locator('[data-fragment="login-message"]');
    await expect(message).toHaveAttribute('role', 'status');
    await expect(message).toContainText('Session expired');
    await expect(page.locator('#user')).toBeEnabled();
  });

  test('a dead session found by the filter of an open page ends the same way', async ({
    page,
    context,
  }) => {
    await page.goto('/login');
    await signIn(page, 'root', PASSWORD);
    await expect(page).toHaveURL(/\/projects$/);
    const baseURL = String(test.info().project.use.baseURL);
    await context.addCookies([{ name: SESSION_COOKIE, value: 'dead-session', url: baseURL }]);
    await page.locator('#ps').fill('fancy');
    await expect(page).toHaveURL(/\/login\?expired=1$/);
  });

  test('there is no standalone session expired page', async ({ page }) => {
    expect((await page.request.get('/session-expired')).status()).toBe(404);
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
    for (const path of ['/login', '/login?expired=1']) {
      const response = await page.goto(path);
      expect(response?.headers()['content-security-policy']).toContain("default-src 'self'");
      const toggle = page.locator('[data-js="theme-toggle"]');
      await expect(toggle).toHaveCount(1);
      await toggle.click();
      await toggle.click();
      expect(await page.evaluate(() => window.cspViolations), path).toEqual([]);
    }
  });
});
