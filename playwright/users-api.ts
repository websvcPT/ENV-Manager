import type { APIRequestContext } from '@playwright/test';

import { csrfHeaders, idLinks, unescapeHtml } from './http';

const PASSWORD = /id="secret" readonly value="([^"]+)"/;

/** The sign-in of a user: the email, which the seed builds from the name and this domain. */
export function seededEmail(name: string): string {
  return `${name}@example.test`;
}

export interface CreatedUser {
  id: number;
  name: string;
  email: string;
  password: string;
}

/**
 * Users matching a search as [id, name] pairs, read from the table fragment as the caller. The
 * list is paged, so a spec that wants one user searches for its name or email instead of reading
 * the first page.
 */
export async function listUsers(
  request: APIRequestContext,
  search = '',
): Promise<Array<[number, string]>> {
  const response = await request.get(`/users/table?q=${encodeURIComponent(search)}`, {
    headers: { 'HX-Request': 'true' },
  });
  return idLinks(await response.text(), '/users');
}

/**
 * Create a user through the same form post the dialog sends, as the caller of `request` (the
 * superadmin), and return the id and the generated password the answer shows once. `grants` are
 * form values such as `project:3`.
 */
export async function createUser(
  request: APIRequestContext,
  name: string,
  group: 'superadmin' | 'admin' | 'viewer' = 'viewer',
  grants: string[] = [],
): Promise<CreatedUser> {
  const email = seededEmail(name);
  const body = new URLSearchParams({ name, email, group });
  for (const grant of grants) {
    body.append('grant', grant);
  }
  const response = await request.post('/users', {
    data: body.toString(),
    headers: {
      ...(await csrfHeaders(request)),
      'Content-Type': 'application/x-www-form-urlencoded',
    },
  });
  const shown = PASSWORD.exec(await response.text());
  if (!response.ok() || !shown) {
    throw new Error(`creating user ${name} failed with status ${response.status()}`);
  }
  const found = (await listUsers(request, email)).find(([, listed]) => listed === name);
  if (!found) {
    throw new Error(`user ${name} was created but is not listed`);
  }
  return { id: found[0], name, email, password: unescapeHtml(shown[1]) };
}

/**
 * Disable a user through the confirmed disable post. Users cannot be deleted in the application,
 * so this is how a spec cleans up after itself (disabling a disabled user changes nothing); the
 * rows themselves are removed later by `envmanager dev cleanup`, which `make e2e` runs before
 * the specs, and finds them by the `e2e-u-` start of the email that `createUser` gives them.
 */
export async function disableUser(request: APIRequestContext, id: number): Promise<void> {
  const response = await request.post(`/users/${id}/disable`, {
    form: { from: 'list' },
    headers: await csrfHeaders(request),
  });
  if (!response.ok()) {
    throw new Error(`disabling user ${id} failed with status ${response.status()}`);
  }
}

/**
 * A name no other run or project uses, so specs never meet each other's users. The email of a
 * user made with it starts with `e2e-u-`, which is what the development cleanup matches on.
 */
export function uniqueUserName(projectName: string): string {
  const suffix = Math.random().toString(36).slice(2, 7);
  return `e2e-u-${Date.now()}-${suffix}-${projectName}`;
}
