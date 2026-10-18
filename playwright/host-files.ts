import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';

/**
 * The folder the stack's app container mounts at /srv/deployments (the browser container mounts
 * the same folder at the same path, as the same user). Deploy specs root their deployments in a
 * folder of their own below it, so two specs running in parallel never share a deploy lock and
 * every file a deploy wrote can be removed again.
 */
export const HOST_ROOT = '/srv/deployments';
export const HOST_FOLDER_PREFIX = 'e2e-host-';

export interface HostFolder {
  /** The deployment root as the app sees it. */
  root: string;
  /** Path of a file below the root. */
  file(relative: string): string;
  write(relative: string, content: string): void;
  read(relative: string): string;
  /** Names in a folder below the root (the root itself by default), sorted. */
  list(relative?: string): string[];
  /** Remove a file or folder below the root, for a spec that needs a deploy to fail. */
  removeBelow(relative: string): void;
  remove(): void;
}

/** A new deployment root with an `environments` folder, removed again by `remove()`. */
export function createHostFolder(): HostFolder {
  const root = mkdtempSync(join(HOST_ROOT, HOST_FOLDER_PREFIX));
  mkdirSync(join(root, 'environments'));
  return {
    root,
    file: (relative) => join(root, relative),
    write: (relative, content) => writeFileSync(join(root, relative), content, 'utf8'),
    read: (relative) => readFileSync(join(root, relative), 'utf8'),
    list: (relative = '') => readdirSync(join(root, relative)).sort(),
    removeBelow: (relative) => rmSync(join(root, relative), { recursive: true, force: true }),
    remove: () => rmSync(root, { recursive: true, force: true }),
  };
}

/** Remove the folders an interrupted run left behind. */
export function removeLeftoverHostFolders(): void {
  if (!existsSync(HOST_ROOT)) {
    return;
  }
  for (const name of readdirSync(HOST_ROOT)) {
    if (name.startsWith(HOST_FOLDER_PREFIX)) {
      rmSync(join(HOST_ROOT, name), { recursive: true, force: true });
    }
  }
}
