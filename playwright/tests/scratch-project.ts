import { test as base } from '@playwright/test';

import { createProject, deleteProjectIfPresent } from '../projects-api';

export interface ScratchProject {
  id: number;
  name: string;
}

/**
 * The base test plus a `scratch` fixture: an empty project created before the test and deleted
 * after it, even when the test renamed it or already deleted it.
 */
export const test = base.extend<{ scratch: ScratchProject }>({
  scratch: async ({ request }, use, testInfo) => {
    const suffix = Math.random().toString(36).slice(2, 7);
    const name = `e2e-detail-${Date.now()}-${suffix}-${testInfo.project.name}`;
    const id = await createProject(request, name);
    try {
      await use({ id, name });
    } finally {
      await deleteProjectIfPresent(request, id);
    }
  },
});

export { expect } from '@playwright/test';
