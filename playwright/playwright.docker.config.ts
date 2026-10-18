import { defineConfig } from '@playwright/test';

import base from './playwright.config';

/**
 * The specs in docker-tests need a usable Docker socket, so the default run (`make e2e`, against
 * the stack without one) never lists them. `make e2e-demo` runs them against the stack with the
 * demo override, whose app container reaches the host daemon. They share one demo stack folder,
 * its files and its containers, so they run one at a time.
 */
export default defineConfig({ ...base, testDir: './docker-tests', fullyParallel: false, workers: 1 });
