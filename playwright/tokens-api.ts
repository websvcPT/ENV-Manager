import type { APIRequestContext } from '@playwright/test';

import { csrfHeaders } from './projects-api';

const TOKEN_ROW = /<tr data-fragment="token-row" id="token-(\d+)">(.*?)<\/tr>/gs;
const NAME = /<span class="font-mono text-\[13px\]">([^<]+)<\/span>/;
const SECRET = /id="secret" readonly value="([^"]+)"/;

export interface CreatedToken {
  id: number;
  name: string;
  secret: string;
}

/** Tokens in the caller's table as [id, name] pairs, read from the table fragment. */
export async function listTokens(request: APIRequestContext): Promise<Array<[number, string]>> {
  const response = await request.get('/tokens/table', { headers: { 'HX-Request': 'true' } });
  const html = await response.text();
  return [...html.matchAll(TOKEN_ROW)].flatMap((match) => {
    const name = NAME.exec(match[2]);
    return name ? [[Number(match[1]), name[1]] as [number, string]] : [];
  });
}

/**
 * Create a token through the same form post the dialog sends, as the caller of `request`, and
 * return its id and the secret the answer shows once. Tokens cannot be deleted, so callers
 * create them only as scratch users, whose tokens are revoked when the user is disabled.
 */
export async function createToken(
  request: APIRequestContext,
  name: string,
  operations: string[] = ['deploy'],
): Promise<CreatedToken> {
  const body = new URLSearchParams({ name });
  for (const operation of operations) {
    body.append('operation', operation);
  }
  const response = await request.post('/tokens', {
    data: body.toString(),
    headers: {
      ...(await csrfHeaders(request)),
      'Content-Type': 'application/x-www-form-urlencoded',
    },
  });
  const shown = SECRET.exec(await response.text());
  if (!response.ok() || !shown) {
    throw new Error(`creating token ${name} failed with status ${response.status()}`);
  }
  const found = (await listTokens(request)).find(([, listed]) => listed === name);
  if (!found) {
    throw new Error(`token ${name} was created but is not listed`);
  }
  return { id: found[0], name, secret: shown[1] };
}
