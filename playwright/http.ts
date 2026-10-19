import type { APIRequestContext } from '@playwright/test';

const ENTITIES: Record<string, string> = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&#34;': '"',
  '&#39;': "'",
};

/** Text of an HTML answer with the entities the server's escaping writes turned back. */
export function unescapeHtml(text: string): string {
  return text.replace(/&(amp|lt|gt|#34|#39);/g, (entity) => ENTITIES[entity]);
}

/** The headers of an htmx post as the signed in caller of `request`, CSRF token included. */
export async function csrfHeaders(request: APIRequestContext): Promise<Record<string, string>> {
  const state = await request.storageState();
  const token = state.cookies.find((cookie) => cookie.name === 'csrftoken')?.value ?? '';
  return { 'HX-Request': 'true', 'X-CSRFToken': token };
}

/**
 * The `[id, text]` pairs of the links `<a href="<base>/<id>">text</a>` in a page or fragment,
 * for example `idLinks(html, '/projects')`. A link with markup inside is not listed.
 */
export function idLinks(html: string, base: string): Array<[number, string]> {
  const pattern = new RegExp(`<a href="${base}/(\\d+)"[^>]*>([^<]+)</a>`, 'g');
  return [...html.matchAll(pattern)].map((match) => [Number(match[1]), unescapeHtml(match[2])]);
}
