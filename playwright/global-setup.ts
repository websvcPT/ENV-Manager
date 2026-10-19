import { request, type FullConfig } from '@playwright/test';

import { HOST_FOLDER_PREFIX, HOST_ROOT, removeLeftoverHostFolders } from './host-files';
import { removeProjectsMatching } from './projects-api';
import { removeAllowedPathsWithPrefix } from './settings-api';
import { seededEmail } from './users-api';

export const SUPERADMIN_STATE = '.auth/superadmin.json';

/** Seeded users that get their own stored session, by the file the specs load. */
export const SESSION_USERS = {
  admin: 'admin',
  viewer: 'viewer',
  deploymentAdmin: 'deployment_admin',
  envScopedAdmin: 'env_scoped_admin',
  envScopedViewer: 'env_scoped_viewer',
  ungrantedAdmin: 'ungranted_admin',
} as const;

export function stateFile(user: keyof typeof SESSION_USERS): string {
  return `.auth/${SESSION_USERS[user]}.json`;
}

async function signInAndStore(
  baseURL: string | undefined,
  email: string,
  password: string,
  path: string,
): Promise<void> {
  const context = await request.newContext({ baseURL });
  await context.get('/healthz');
  const issued = await context.storageState();
  const csrfToken = issued.cookies.find((cookie) => cookie.name === 'csrftoken')?.value;
  if (!csrfToken) {
    throw new Error('the application did not issue a CSRF cookie');
  }
  const response = await context.post('/login', {
    form: { identifier: email, password, csrf_token: csrfToken },
    maxRedirects: 0,
  });
  if (response.status() !== 303) {
    throw new Error(`login as ${email} failed with status ${response.status()}`);
  }
  await context.storageState({ path });
  await context.dispose();
}

/** Allowed paths that specs add are folders of the host root named e2e-host-...; remove leftovers. */
async function removeLeftoverAllowedPaths(baseURL: string | undefined): Promise<void> {
  const context = await request.newContext({ baseURL, storageState: SUPERADMIN_STATE });
  await removeAllowedPathsWithPrefix(context, `${HOST_ROOT}/${HOST_FOLDER_PREFIX}`);
  await context.dispose();
}

/** Projects that specs create are named e2e-...; remove those an interrupted run left behind. */
async function removeLeftoverProjects(baseURL: string | undefined): Promise<void> {
  const context = await request.newContext({ baseURL, storageState: SUPERADMIN_STATE });
  await removeProjectsMatching(context, 'e2e-', /^e2e-(detail-)?\d+-/);
  await context.dispose();
}

/**
 * Sign in through the real login route and store the sessions, so every spec starts
 * authenticated: the superadmin for every spec by default, plus one stored session per other
 * seeded role for the specs that check what each role sees. The development dataset is seeded
 * by `make e2e` before this runs.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0].use.baseURL;
  const password = process.env.E2E_PASSWORD ?? 'dev-only-password';
  const superadmin = process.env.E2E_USER ?? seededEmail('root');
  await signInAndStore(baseURL, superadmin, password, SUPERADMIN_STATE);
  for (const user of Object.keys(SESSION_USERS) as Array<keyof typeof SESSION_USERS>) {
    await signInAndStore(baseURL, seededEmail(SESSION_USERS[user]), password, stateFile(user));
  }
  await removeLeftoverProjects(baseURL);
  await removeLeftoverAllowedPaths(baseURL);
  removeLeftoverHostFolders();
}
