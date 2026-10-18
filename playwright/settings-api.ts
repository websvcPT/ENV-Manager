import type { APIRequestContext } from '@playwright/test';

import { csrfHeaders } from './projects-api';

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
