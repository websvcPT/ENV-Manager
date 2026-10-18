import { defineConfig } from '@playwright/test';

import base from './playwright.config';

/**
 * The specs in docker-tests need a usable Docker socket, so the default run (`make e2e`, against
 * the stack without one) never lists them. `make e2e-demo` runs them against the stack with the
 * demo override, whose app container reaches the host daemon.
 */
export default defineConfig({ ...base, testDir: './docker-tests' });
