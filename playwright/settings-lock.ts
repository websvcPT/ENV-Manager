import { mkdirSync, rmSync, statSync } from 'node:fs';

import { HOST_FOLDER_PREFIX, HOST_ROOT } from './host-files';

/*
 * Specs that change a stored setting hold this lock, so the desktop and phone projects never
 * change the same settings at once and each can read back exactly what it saved. The lock is a
 * folder in the host root (made with one atomic call, shared by every worker of the run) whose
 * name carries the prefix that `removeLeftoverHostFolders` already clears after an interrupted
 * run.
 */
const LOCK = `${HOST_ROOT}/${HOST_FOLDER_PREFIX}settings-lock`;
const STALE_AFTER_MS = 3 * 60_000;
const GIVE_UP_AFTER_MS = 4 * 60_000;
const POLL_MS = 250;

export async function acquireSettingsLock(): Promise<void> {
  const started = Date.now();
  for (;;) {
    try {
      mkdirSync(LOCK);
      return;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
        throw error;
      }
    }
    try {
      if (Date.now() - statSync(LOCK).mtimeMs > STALE_AFTER_MS) {
        rmSync(LOCK, { recursive: true, force: true });
        continue;
      }
    } catch {
      continue;
    }
    if (Date.now() - started > GIVE_UP_AFTER_MS) {
      throw new Error('the settings lock was never released');
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

export function releaseSettingsLock(): void {
  rmSync(LOCK, { recursive: true, force: true });
}
