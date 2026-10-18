import type { APIRequestContext, Browser } from '@playwright/test';

import { SUPERADMIN_STATE } from './global-setup';
import { csrfHeaders } from './projects-api';
import { serviceId } from './services-api';

function environmentLink(name: string): RegExp {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`<a href="/environments/(\\d+)"[^>]*>${escaped}</a>`);
}

/** Id of an environment of a service, read from the service's environments panel. */
export async function environmentIdIn(
  request: APIRequestContext,
  service: number,
  name: string,
): Promise<number> {
  const response = await request.get(`/services/${service}/environments`, {
    headers: { 'HX-Request': 'true' },
  });
  const found = environmentLink(name).exec(await response.text());
  if (!found) {
    throw new Error(`environment ${name} is not listed for service ${service}`);
  }
  return Number(found[1]);
}

/**
 * Id of a seeded environment by project, deployment, service and file name, looked up as the
 * superadmin so a spec running as any role can use it.
 */
export async function seededEnvironmentId(
  browser: Browser,
  project: string,
  deployment: string,
  service: string,
  name: string,
): Promise<number> {
  const serviceIdentifier = await serviceId(browser, project, deployment, service);
  const context = await browser.newContext({ storageState: SUPERADMIN_STATE });
  try {
    return await environmentIdIn(context.request, serviceIdentifier, name);
  } finally {
    await context.close();
  }
}

/**
 * Create an environment with pasted content through the add environment form post, as the
 * caller of `request` (the superadmin), and return its id. `distPath` links a dist file below
 * the deployment root.
 */
export async function createEnvironment(
  request: APIRequestContext,
  service: number,
  name: string,
  content: string,
  distPath = '',
): Promise<number> {
  const response = await request.post(`/services/${service}/environments/new`, {
    form: {
      service: String(service),
      new_service: '',
      name,
      target_path: `environments/${name}`,
      dist_path: distPath,
      file_mode: '',
      file_owner: '',
      file_group: '',
      source: 'paste',
      content,
    },
    headers: await csrfHeaders(request),
  });
  if (!response.ok() || !response.headers()['hx-redirect']?.includes('added=environment')) {
    throw new Error(`creating environment ${name} failed with status ${response.status()}`);
  }
  return environmentIdIn(request, service, name);
}

/** Save a raw text as the next version, the way the editor posts it. */
export async function saveRaw(
  request: APIRequestContext,
  environment: number,
  baseVersion: number,
  content: string,
): Promise<void> {
  const response = await request.post(`/environments/${environment}/versions`, {
    form: { mode: 'raw', base_version: String(baseVersion), comment: '', content },
    headers: await csrfHeaders(request),
  });
  if (!response.headers()['hx-redirect']) {
    throw new Error(`saving environment ${environment} failed with status ${response.status()}`);
  }
}
