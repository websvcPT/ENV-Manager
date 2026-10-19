import { request, type APIRequestContext } from '@playwright/test';

/**
 * The development mail catcher (smtp4dev) the stack's SMTP settings point at. Its web API is
 * reachable from the browser containers over the Compose network; the specs read back what the
 * application sent.
 */
export const MAIL_URL = process.env.E2E_MAIL_URL ?? 'http://smtp4dev';

export interface CaughtMail {
  id: string;
  to: string[];
  subject: string;
  receivedDate: string;
}

interface MailList {
  results: CaughtMail[];
}

async function catcher(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: MAIL_URL });
}

/** Every mail the catcher holds, newest first. */
export async function caughtMail(): Promise<CaughtMail[]> {
  const context = await catcher();
  try {
    const response = await context.get('/api/messages?pageSize=200');
    return ((await response.json()) as MailList).results;
  } finally {
    await context.dispose();
  }
}

/** The plain text part of a caught mail, decoded by the catcher. */
export async function plainText(id: string): Promise<string> {
  const context = await catcher();
  try {
    return await (await context.get(`/api/messages/${id}/plaintext`)).text();
  } finally {
    await context.dispose();
  }
}

/** The HTML part of a caught mail. */
export async function htmlText(id: string): Promise<string> {
  const context = await catcher();
  try {
    return await (await context.get(`/api/messages/${id}/html`)).text();
  } finally {
    await context.dispose();
  }
}

/** Wait until a mail that the test accepts has arrived, and return it. */
export async function waitForMail(
  accepts: (mail: CaughtMail) => boolean,
  what: string,
  timeoutMs = 15_000,
): Promise<CaughtMail> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = (await caughtMail()).find(accepts);
    if (found) {
      return found;
    }
    if (Date.now() > deadline) {
      throw new Error(`no mail arrived for ${what}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

/** Wait for a mail to an address whose subject contains the text. */
export async function waitForMailTo(address: string, subject: string): Promise<CaughtMail> {
  return waitForMail(
    (mail) => mail.to.includes(address) && mail.subject.includes(subject),
    `${address} about "${subject}"`,
  );
}
