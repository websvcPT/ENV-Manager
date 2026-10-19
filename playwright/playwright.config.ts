import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.E2E_BASE_URL ?? 'http://envmanager:8080';

export default defineConfig({
  testDir: './tests',
  outputDir: './test-results',
  globalSetup: './global-setup.ts',
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL,
    storageState: '.auth/superadmin.json',
    trace: 'retain-on-failure',
  },
  // A test that only applies to one layout carries a tag, @wide (from the md breakpoint up) or
  // @narrow (below it), and the other project leaves it out instead of reporting it skipped.
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 900 } },
      grepInvert: /@narrow/,
    },
    {
      name: 'phone',
      use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' },
      grepInvert: /@wide/,
    },
  ],
});
