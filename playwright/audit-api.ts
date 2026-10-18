import type { APIRequestContext } from '@playwright/test';

/**
 * Entry ids of the first page of an audit view (query string without the leading question
 * mark), newest first, read from the table fragment the way the filter form requests it.
 */
export async function auditEntryIds(request: APIRequestContext, query = ''): Promise<number[]> {
  const response = await request.get(`/audit/table?${query}`, {
    headers: { 'HX-Request': 'true' },
  });
  if (!response.ok()) {
    throw new Error(`reading the audit log failed with status ${response.status()}`);
  }
  const html = await response.text();
  return [...html.matchAll(/id="audit-entry-(\d+)"/g)].map((match) => Number(match[1]));
}
