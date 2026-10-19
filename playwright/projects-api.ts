import type { APIRequestContext } from '@playwright/test';

import { csrfHeaders, idLinks } from './http';

/** Projects in the caller's list as [id, name] pairs, optionally filtered by a name search. */
export async function listProjects(
  request: APIRequestContext,
  query = '',
): Promise<Array<[number, string]>> {
  const response = await request.get(`/projects/table?q=${encodeURIComponent(query)}`, {
    headers: { 'HX-Request': 'true' },
  });
  return idLinks(await response.text(), '/projects');
}

/** Create a project through the same form post the dialog sends and return its id. */
export async function createProject(request: APIRequestContext, name: string): Promise<number> {
  const response = await request.post('/projects', {
    form: { name },
    headers: await csrfHeaders(request),
  });
  if (!response.ok()) {
    throw new Error(`creating project ${name} failed with status ${response.status()}`);
  }
  const found = (await listProjects(request, name)).find(([, listed]) => listed === name);
  if (!found) {
    throw new Error(`project ${name} was created but is not listed`);
  }
  return found[0];
}

/** Delete a project by id and current name through the confirmed delete post. */
export async function deleteProject(
  request: APIRequestContext,
  id: number,
  name: string,
): Promise<void> {
  const response = await request.post(`/projects/${id}/delete`, {
    form: { confirm_name: name },
    headers: await csrfHeaders(request),
  });
  if (!response.ok()) {
    throw new Error(`deleting project ${name} failed with status ${response.status()}`);
  }
}

/** Delete a project by id when it still exists, whatever its name is now (it may be renamed). */
export async function deleteProjectIfPresent(
  request: APIRequestContext,
  id: number,
): Promise<void> {
  const page = await request.get(`/projects/${id}`);
  if (!page.ok()) {
    return;
  }
  const heading = /<h1[^>]*>([^<]+)<\/h1>/.exec(await page.text());
  if (heading) {
    await deleteProject(request, id, heading[1]);
  }
}

/** Delete every listed project whose name matches the pattern. */
export async function removeProjectsMatching(
  request: APIRequestContext,
  search: string,
  pattern: RegExp,
): Promise<void> {
  for (const [id, name] of await listProjects(request, search)) {
    if (pattern.test(name)) {
      await deleteProject(request, id, name);
    }
  }
}
