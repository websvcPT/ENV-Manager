import type { APIRequestContext } from '@playwright/test';

import { csrfHeaders } from './projects-api';

const USER_LINK = /<a href="\/users\/(\d+)"[^>]*>([^<]+)<\/a>/g;
const PASSWORD = /id="secret" readonly value="([^"]+)"/;
const ENTITIES: Record<string, string> = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&#34;': '"',
  '&#39;': "'",
};

export interface CreatedUser {
  id: number;
  name: string;
  email: string;
  password: string;
}

function unescapeHtml(text: string): string {
  return text.replace(/&(amp|lt|gt|#34|#39);/g, (entity) => ENTITIES[entity]);
}

/** Users in the list as [id, name] pairs, read from the table fragment as the caller. */
export async function listUsers(request: APIRequestContext): Promise<Array<[number, string]>> {
  const response = await request.get('/users/table', { headers: { 'HX-Request': 'true' } });
  const html = await response.text();
  return [...html.matchAll(USER_LINK)].map((match) => [Number(match[1]), match[2]]);
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
  const email = `${name}@example.test`;
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
  const found = (await listUsers(request)).find(([, listed]) => listed === name);
  if (!found) {
    throw new Error(`user ${name} was created but is not listed`);
  }
  return { id: found[0], name, email, password: unescapeHtml(shown[1]) };
}

/**
 * Disable a user through the confirmed disable post. Users cannot be deleted, so this is how a
 * spec cleans up after itself; disabling a disabled user changes nothing.
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

/** A name no other run or project uses, so specs never meet each other's users. */
export function uniqueUserName(projectName: string): string {
  const suffix = Math.random().toString(36).slice(2, 7);
  return `e2e-u-${Date.now()}-${suffix}-${projectName}`;
}
