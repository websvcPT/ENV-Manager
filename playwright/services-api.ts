import type { Browser } from '@playwright/test';

import { deploymentId } from './deployments-api';
import { SUPERADMIN_STATE } from './global-setup';

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
