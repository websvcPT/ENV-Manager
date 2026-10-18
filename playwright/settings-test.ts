import { test as base } from '@playwright/test';

import { acquireSettingsLock, releaseSettingsLock } from './settings-lock';

/*
 * `test` with a `settingsLock` fixture that a spec asks for when it changes a stored setting or
 * takes a backup: the desktop and phone projects then never do it at the same time, and each can
 * read back exactly what it saved. Asking for the fixture takes the lock for the length of the
 * test and releases it afterwards.
 */
export const test = base.extend<{ settingsLock: void }>({
  settingsLock: [
    // Playwright reads the fixture's dependencies from this destructuring, which is empty.
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      await acquireSettingsLock();
      try {
        await use();
      } finally {
        releaseSettingsLock();
      }
    },
    { auto: false, timeout: 5 * 60_000 },
  ],
});
