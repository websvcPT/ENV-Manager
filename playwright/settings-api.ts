import type { APIRequestContext } from '@playwright/test';

import { csrfHeaders } from './http';

const PATH_ROW = /<tr data-fragment="path-row" id="path-(\d+)">.*?<span class="font-mono text-\[13px\]">([^<]+)<\/span>/gs;

/** The allowed paths in the list as [id, path] pairs. */
export async function listAllowedPaths(request: APIRequestContext): Promise<Array<[number, string]>> {
  const response = await request.get('/settings/paths/table', { headers: { 'HX-Request': 'true' } });
  const html = await response.text();
  return [...html.matchAll(PATH_ROW)].map((match) => [Number(match[1]), match[2]]);
}

/** Remove an allowed path by id through the confirmed remove post. */
export async function removeAllowedPath(request: APIRequestContext, id: number): Promise<void> {
  const response = await request.post(`/settings/paths/${id}/remove`, {
    headers: await csrfHeaders(request),
  });
  if (!response.ok()) {
    throw new Error(`removing allowed path ${id} failed with status ${response.status()}`);
  }
}

/** Add an allowed path through the same post the dialog sends. */
export async function addAllowedPath(request: APIRequestContext, path: string): Promise<void> {
  const response = await request.post('/settings/paths', {
    form: { path },
    headers: await csrfHeaders(request),
  });
  if (response.status() !== 204) {
    throw new Error(`adding allowed path ${path} failed with status ${response.status()}`);
  }
}

/** Remove every allowed path that starts with the prefix, such as one a spec left behind. */
export async function removeAllowedPathsWithPrefix(
  request: APIRequestContext,
  prefix: string,
): Promise<void> {
  for (const [id, path] of await listAllowedPaths(request)) {
    if (path.startsWith(prefix)) {
      await removeAllowedPath(request, id);
    }
  }
}

const REGISTRY_ROW = /<tr data-fragment="registry-row" id="registry-(\d+)">.*?<span class="wrap-anywhere font-mono text-\[13px\]">([^<]+)<\/span>/gs;

/** The registry logins in the list as [id, host] pairs. */
export async function listRegistries(request: APIRequestContext): Promise<Array<[number, string]>> {
  const response = await request.get('/settings/registries/table', {
    headers: { 'HX-Request': 'true' },
  });
  const html = await response.text();
  return [...html.matchAll(REGISTRY_ROW)].map((match) => [Number(match[1]), match[2]]);
}

/** Add a registry login through the same post the dialog sends. */
export async function addRegistry(
  request: APIRequestContext,
  host: string,
  username: string,
  token: string,
): Promise<number> {
  const response = await request.post('/settings/registries', {
    form: { host, username, token },
    headers: await csrfHeaders(request),
  });
  if (response.status() !== 204) {
    throw new Error(`adding registry ${host} failed with status ${response.status()}`);
  }
  const found = (await listRegistries(request)).find(([, listed]) => listed === host.toLowerCase());
  if (found === undefined) {
    throw new Error(`registry ${host} is not in the list after adding it`);
  }
  return found[0];
}

/** Remove a registry login by id through the confirmed remove post. */
export async function removeRegistry(request: APIRequestContext, id: number): Promise<void> {
  const response = await request.post(`/settings/registries/${id}/remove`, {
    headers: await csrfHeaders(request),
  });
  if (!response.ok()) {
    throw new Error(`removing registry ${id} failed with status ${response.status()}`);
  }
}

/** Remove every registry login whose host starts with the prefix, such as one a spec left behind. */
export async function removeRegistriesWithPrefix(
  request: APIRequestContext,
  prefix: string,
): Promise<void> {
  for (const [id, host] of await listRegistries(request)) {
    if (host.startsWith(prefix)) {
      await removeRegistry(request, id);
    }
  }
}
