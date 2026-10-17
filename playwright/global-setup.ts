import { request, type FullConfig } from '@playwright/test';

const STORAGE_STATE = '.auth/superadmin.json';

/**
 * Sign in as the seeded superadmin through the real login route and store the session, so every
 * spec starts authenticated. The development dataset is seeded by `make e2e` before this runs.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0].use.baseURL;
  const identifier = process.env.E2E_USER ?? 'root';
  const password = process.env.E2E_PASSWORD ?? 'dev-only-password';

  const context = await request.newContext({ baseURL });
  await context.get('/healthz');
  const issued = await context.storageState();
  const csrfToken = issued.cookies.find((cookie) => cookie.name === 'csrftoken')?.value;
  if (!csrfToken) {
    throw new Error('the application did not issue a CSRF cookie');
  }
  const response = await context.post('/login', {
    form: { identifier, password, csrf_token: csrfToken },
    maxRedirects: 0,
  });
  if (response.status() !== 303) {
    throw new Error(`login as ${identifier} failed with status ${response.status()}`);
  }
  await context.storageState({ path: STORAGE_STATE });
  await context.dispose();
}
