import { defineConfig, devices } from '@playwright/test';

/**
 * The single sign-on proof (`make e2e-sso`). It runs against its own app instance and a
 * Keycloak that the target starts with a fresh seeded database, never the development stack, so
 * there is no global setup and no stored session: the specs sign in through the real pages. The
 * tests of one file share the instance's single sign-on settings and run in order, one worker.
 */
export default defineConfig({
  testDir: './sso-tests',
  outputDir: './test-results/sso',
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://envmanager-sso:8080',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 900 } },
    },
    {
      name: 'phone',
      use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' },
    },
  ],
});
