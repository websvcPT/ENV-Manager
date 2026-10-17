import type { APIRequestContext, Browser } from '@playwright/test';

import { SUPERADMIN_STATE } from './global-setup';
import { csrfHeaders, listProjects } from './projects-api';

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

/**
 * Create a deployment in a project through the same form post the page sends, as the caller of
 * `request` (the superadmin), and return its id.
 */
export async function createDeployment(
  request: APIRequestContext,
  projectId: number,
  name: string,
  root: string,
): Promise<number> {
  const response = await request.post(`/projects/${projectId}/deployments`, {
    form: {
      name,
      root,
      dist_missing_policy: 'block',
      drift_policy: 'halt',
      missing_target_policy: 'create',
      file_mode: '0600',
      file_owner: '',
      file_group: '',
    },
    headers: await csrfHeaders(request),
  });
  const target = response.headers()['hx-redirect'] ?? '';
  const found = /^\/deployments\/(\d+)$/.exec(target);
  if (!response.ok() || !found) {
    throw new Error(`creating deployment ${name} failed with status ${response.status()}`);
  }
  return Number(found[1]);
}
