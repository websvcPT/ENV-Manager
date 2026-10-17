import type { Browser } from '@playwright/test';

import { SUPERADMIN_STATE } from './global-setup';
import { listProjects } from './projects-api';

const DEPLOYMENT_LINK = /<a href="\/deployments\/(\d+)"[^>]*>([^<]+)<\/a>/g;

/**
 * Id of a seeded deployment by project and deployment name, looked up as the superadmin so a
 * spec running as any role can use it.
 */
export async function deploymentId(
  browser: Browser,
  projectName: string,
  name: string,
): Promise<number> {
  const context = await browser.newContext({ storageState: SUPERADMIN_STATE });
  try {
    const project = (await listProjects(context.request, projectName)).find(
      ([, listed]) => listed === projectName,
    );
    if (!project) {
      throw new Error(`seeded project ${projectName} is missing`);
    }
    const response = await context.request.get(`/projects/${project[0]}/deployments`, {
      headers: { 'HX-Request': 'true' },
    });
    const html = await response.text();
    const found = [...html.matchAll(DEPLOYMENT_LINK)].find((match) => match[2] === name);
    if (!found) {
      throw new Error(`seeded deployment ${projectName}/${name} is missing`);
    }
    return Number(found[1]);
  } finally {
    await context.close();
  }
}
