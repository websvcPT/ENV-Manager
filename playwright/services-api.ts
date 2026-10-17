import type { APIRequestContext, Browser } from '@playwright/test';

import { deploymentId } from './deployments-api';
import { SUPERADMIN_STATE } from './global-setup';
import { csrfHeaders } from './projects-api';

const SERVICE_LINK = /<a href="\/services\/(\d+)"[^>]*>([^<]+)<\/a>/g;

/**
 * Id of a seeded service by project, deployment and service name, read from the deployment page
 * as the superadmin so a spec running as any role can use it.
 */
export async function serviceId(
  browser: Browser,
  projectName: string,
  deploymentName: string,
  name: string,
): Promise<number> {
  const deployment = await deploymentId(browser, projectName, deploymentName);
  const context = await browser.newContext({ storageState: SUPERADMIN_STATE });
  try {
    const response = await context.request.get(`/deployments/${deployment}/services`, {
      headers: { 'HX-Request': 'true' },
    });
    const html = await response.text();
    const found = [...html.matchAll(SERVICE_LINK)].find((match) => match[2] === name);
    if (!found) {
      throw new Error(`seeded service ${projectName}/${deploymentName}/${name} is missing`);
    }
    return Number(found[1]);
  } finally {
    await context.close();
  }
}

/**
 * Create a service in a deployment through the same form post the page sends, as the caller of
 * `request` (the superadmin), and return its id.
 */
export async function createService(
  request: APIRequestContext,
  deployment: number,
  name: string,
): Promise<number> {
  const response = await request.post(`/deployments/${deployment}/services/new`, {
    form: { name },
    headers: await csrfHeaders(request),
  });
  const found = /^\/services\/(\d+)\?added=service$/.exec(response.headers()['hx-redirect'] ?? '');
  if (!response.ok() || !found) {
    throw new Error(`creating service ${name} failed with status ${response.status()}`);
  }
  return Number(found[1]);
}
