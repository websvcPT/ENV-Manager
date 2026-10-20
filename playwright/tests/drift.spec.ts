import { rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';

import type { APIRequestContext, Locator, Page } from '@playwright/test';

import { createDeployment } from '../deployments-api';
import { createEnvironment, saveRaw, seededEnvironmentId } from '../environments-api';
import { stateFile } from '../global-setup';
import { expectItemHeader, pageWithoutLocation } from '../item-header';
import { createHostFolder, type HostFolder } from '../host-files';
import { csrfHeaders } from '../http';
import { createService } from '../services-api';
import { horizontalOverflow } from '../viewport';
import { expect, test as scratchTest } from './scratch-project';

/*
 * The drift view against the default stack, which has no Docker socket: every state of the three
 * columns, the audited reveal with its 30 second removal, importing the target as a version,
 * overwriting it through the confirmation, and what each role may do. Every spec roots its
 * deployment in a folder of its own below /srv/deployments (removed afterwards), because an
 * overwrite really deploys. Specs never touch the seeded deployments' files: the roles run on
 * seeded files whose roots do not exist on this stack, which is itself the outside state.
 */

const TARGET_VALUE = 'e2e-disk-Tv51';
const DEPLOYED_VALUE = 'e2e-deployed-Dv52';
const MANAGED_VALUE = 'e2e-managed-Mv53';
const SAME_VALUE = 'e2e-same-Sv54';
const ONLY_DISK_VALUE = 'e2e-only-disk-Od55';
const ALL_VALUES = [TARGET_VALUE, DEPLOYED_VALUE, MANAGED_VALUE, SAME_VALUE, ONLY_DISK_VALUE];

const DEPLOYED = `DRIFT_KEY=${DEPLOYED_VALUE}\nSAME_KEY=${SAME_VALUE}\n`;
const MANAGED = `DRIFT_KEY=${MANAGED_VALUE}\nSAME_KEY=${SAME_VALUE}\n`;
const TARGET = `DRIFT_KEY=${TARGET_VALUE}\nONLY_DISK=${ONLY_DISK_VALUE}\nSAME_KEY=${SAME_VALUE}\n`;
const PEM = '-----BEGIN CERTIFICATE-----\nE2EDRIFTMANAGED\n-----END CERTIFICATE-----\n';
const PEM_DISK = '-----BEGIN CERTIFICATE-----\nE2EDRIFTONDISK\n-----END CERTIFICATE-----\n';

const test = scratchTest.extend<{ host: HostFolder; service: number; drifted: number }>({
  host: async ({}, use) => {
    const host = createHostFolder();
    try {
      await use(host);
    } finally {
      host.remove();
    }
  },
  /** A service in a deployment rooted in the host folder. */
  service: async ({ request, scratch, host }, use) => {
    const deployment = await createDeployment(request, scratch.id, 'drift', host.root, 'block');
    await use(await createService(request, deployment, 'web'));
  },
  /** The environment of a file in the drift state, with every kind of row. */
  drifted: async ({ request, host, service }, use) => {
    await use(await driftedFile(request, host, service));
  },
});

/** Deploy one file of the service as the signed-in superadmin and expect it to succeed. */
async function deploy(request: APIRequestContext, service: number, environment: number) {
  const response = await request.post(`/services/${service}/deploy`, {
    form: { selection: 'explicit', environment: String(environment) },
    headers: await csrfHeaders(request),
  });
  expect(response.ok()).toBe(true);
  expect(await response.text()).toContain('data-tone="ok"');
}

/**
 * A file deployed at DEPLOYED, then saved as MANAGED (version 2) and edited on disk to TARGET: the
 * drift state with every kind of row.
 */
async function driftedFile(
  request: APIRequestContext,
  host: HostFolder,
  service: number,
  name = '.env.app',
): Promise<number> {
  const id = await createEnvironment(request, service, name, DEPLOYED);
  await deploy(request, service, id);
  await saveRaw(request, id, 1, MANAGED);
  host.write(`environments/${name}`, TARGET);
  return id;
}

function url(environment: number): string {
  return `/environments/${environment}/drift`;
}

function header(page: Page): Locator {
  return page.locator('[data-fragment="page-header"]');
}

function badge(page: Page): Locator {
  return header(page).locator('[data-status]');
}

function panel(page: Page): Locator {
  return page.locator('[data-fragment="drift-view"]');
}

function row(page: Page, key: string): Locator {
  return panel(page).locator('[data-fragment="drift-row"]', { hasText: key });
}

function revealButton(page: Page): Locator {
  return panel(page).locator('button[hx-post*="/drift/reveal"]');
}

function overwriteButton(page: Page): Locator {
  return header(page).getByRole('button', { name: /^Overwrite target/ });
}

function importButton(page: Page): Locator {
  return header(page).getByRole('button', { name: 'Import target as new version' });
}

function toast(page: Page): Locator {
  return page.locator('[data-fragment="toast"]');
}

function notice(page: Page): Locator {
  return page.locator('#drift-notice');
}

async function expectNoValueInPage(page: Page, ...values: string[]): Promise<void> {
  const content = await page.content();
  for (const value of values) {
    expect(content).not.toContain(value);
  }
}

async function expectFits(page: Page): Promise<void> {
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
}

async function historyVersions(page: Page, environment: number): Promise<string[]> {
  await page.goto(`/environments/${environment}/history`);
  return page
    .locator('[data-fragment="version-table"] [data-fragment="version-row"] th[scope="row"]')
    .allTextContents();
}

function backups(host: HostFolder, name: string): string[] {
  return host.list('environments').filter((entry) => entry.startsWith(`${name}.bak-`));
}

test.describe('states', () => {
  test('a file edited on disk after its deploy lists what differs per column, masked', async ({
    page,
    service,
    drifted: id,
  }) => {
    await page.goto(url(id));

    await expectItemHeader(page, 'environment', '.env.app');
    await expect(badge(page)).toHaveAttribute('data-status', 'drift');
    await expect(badge(page)).toHaveText('Drift');
    await expect(header(page)).toContainText(
      'The target file changed outside the app since the last deploy. Choose how to resolve it.',
    );
    await expect(panel(page).locator('h2')).toHaveText('Three-way comparison');
    const columns = panel(page).locator('.bg-head > span');
    await expect(columns.nth(1)).toContainText('Target on disk');
    await expect(columns.nth(1)).toContainText(/hash [0-9a-f]{7}… read \d\d:\d\d/);
    await expect(columns.nth(2)).toContainText('Last deployed v1');
    await expect(columns.nth(2)).toContainText(/hash [0-9a-f]{7}… written \d{4}-\d\d-\d\d/);
    await expect(columns.nth(3)).toContainText('Managed v2');
    await expect(columns.nth(3)).toContainText(/hash [0-9a-f]{7}… latest version/);

    await expect(panel(page).locator('[data-fragment="drift-row"]')).toHaveCount(2);
    const changed = row(page, 'DRIFT_KEY');
    await expect(changed.locator('[data-change="disk-changed"]')).toHaveText('Changed on disk');
    await expect(changed.locator('[data-change="managed-changed"]')).toHaveText(
      'Changed in managed',
    );
    await expect(changed).toHaveClass(/bg-accent-soft/);
    const added = row(page, 'ONLY_DISK');
    await expect(added.locator('[data-change="disk-added"]')).toHaveText('Added on disk');
    await expect(added).toContainText('not set');
    await expect(panel(page)).toContainText('1 unchanged key hidden');
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(0);
    await expect(changed).toContainText('••••••••');
    await expectNoValueInPage(page, ...ALL_VALUES);
    await expect(overwriteButton(page)).toBeVisible();
    await expect(importButton(page)).toBeVisible();
    await expect(header(page).getByRole('link', { name: 'Cancel' })).toHaveAttribute(
      'href',
      `/services/${service}`,
    );
    await expectFits(page);
  });

  test('a file never deployed whose target already has content reads Never deployed', async ({
    page,
    request,
    service,
    host,
  }) => {
    const id = await createEnvironment(request, service, '.env.fresh', MANAGED);
    host.write('environments/.env.fresh', TARGET);
    await page.goto(url(id));

    await expect(badge(page)).toHaveAttribute('data-status', 'never');
    await expect(badge(page)).toHaveText('Never deployed');
    await expect(header(page)).toContainText('was never deployed by the app');
    await expect(panel(page).locator('.bg-head')).toContainText('never deployed');
    await expect(row(page, 'DRIFT_KEY').locator('[data-change="disk-changed"]')).toBeVisible();
    await expect(row(page, 'DRIFT_KEY')).toContainText('not deployed');
    await expect(overwriteButton(page)).toBeVisible();
    await expect(importButton(page)).toBeVisible();
    await expectNoValueInPage(page, ...ALL_VALUES);
  });

  test('a target that matches the deployed version reads No drift and offers no action', async ({
    page,
    request,
    service,
  }) => {
    const id = await createEnvironment(request, service, '.env.clean', DEPLOYED);
    await deploy(request, service, id);
    await saveRaw(request, id, 1, MANAGED);
    await page.goto(url(id));

    await expect(badge(page)).toHaveAttribute('data-status', 'deployed');
    await expect(badge(page)).toHaveText('No drift');
    await expect(header(page)).toContainText(
      'The target file matches the last deployed version. Version 2 is newer and waits for a deploy.',
    );
    await expect(row(page, 'DRIFT_KEY').locator('[data-change="managed-changed"]')).toBeVisible();
    await expect(page.locator('[data-fragment="drift-row"] [data-change^="disk"]')).toHaveCount(0);
    await expect(overwriteButton(page)).toHaveCount(0);
    await expect(importButton(page)).toHaveCount(0);
    await expect(page.locator('#overwrite-dialog')).toHaveCount(0);
    await expect(header(page).getByRole('link', { name: 'Back to service' })).toBeVisible();
  });

  test('a target that matches everything says there is nothing to compare', async ({
    page,
    request,
    service,
  }) => {
    const id = await createEnvironment(request, service, '.env.same', MANAGED);
    await deploy(request, service, id);
    await page.goto(url(id));

    await expect(badge(page)).toHaveText('No drift');
    await expect(header(page)).toContainText('The target file matches the managed version.');
    await expect(panel(page)).toContainText('No keys differ between the columns.');
    await expect(panel(page)).toContainText('2 unchanged keys hidden');
    await expect(revealButton(page)).toHaveCount(0);
  });

  test('a target that differs only in comments is Drift with no variable listed', async ({
    page,
    request,
    service,
    host,
  }) => {
    const id = await createEnvironment(request, service, '.env.notes', 'A=1\nB=2\n');
    await deploy(request, service, id);
    host.write('environments/.env.notes', '# edited by hand\nB=2\nA=1\n');
    await page.goto(url(id));

    await expect(badge(page)).toHaveText('Drift');
    await expect(panel(page)).toContainText(
      'No variables differ. The files differ only in comments, blank lines, line breaks or order.',
    );
    await expect(panel(page).locator('[data-fragment="drift-row"]')).toHaveCount(0);
  });

  test('a missing target or folder reads Target missing and offers no action', async ({
    page,
    request,
    service,
    host,
  }) => {
    const id = await createEnvironment(request, service, '.env.gone', MANAGED);
    await page.goto(url(id));
    await expect(badge(page)).toHaveText('Target missing');
    await expect(header(page)).toContainText('The target file does not exist.');
    await expect(panel(page).locator('.bg-head')).toContainText('file not found');
    await expect(panel(page)).toContainText(
      'Nothing to compare. Only the managed version is available.',
    );
    await expect(panel(page)).not.toContainText('unchanged key');
    await expect(overwriteButton(page)).toHaveCount(0);
    await expect(importButton(page)).toHaveCount(0);
    await expect(revealButton(page)).toHaveCount(0);

    host.removeBelow('environments');
    await page.goto(url(id));
    await expect(badge(page)).toHaveText('Target missing');
    await expect(header(page)).toContainText('The target folder does not exist.');
    expect(await pageWithoutLocation(page)).not.toContain(host.root);
  });

  test('a target that cannot be read reads Cannot read target with a fixed sentence', async ({
    page,
    request,
    service,
    host,
  }) => {
    const link = await createEnvironment(request, service, '.env.link', MANAGED);
    symlinkSync('/etc/hostname', host.file('environments/.env.link'));
    await page.goto(url(link));
    await expect(badge(page)).toHaveAttribute('data-status', 'failed');
    await expect(badge(page)).toHaveText('Cannot read target');
    await expect(header(page)).toContainText('The target is a symbolic link and is not replaced.');
    await expect(panel(page).locator('.bg-head')).toContainText('cannot be read');
    await expect(overwriteButton(page)).toHaveCount(0);
    await expect(importButton(page)).toHaveCount(0);

    const binary = await createEnvironment(request, service, '.env.binary', MANAGED);
    writeFileSync(host.file('environments/.env.binary'), Buffer.from([0x41, 0x3d, 0xff, 0xfe, 0x0a]));
    await page.goto(url(binary));
    await expect(badge(page)).toHaveText('Cannot read target');
    await expect(header(page)).toContainText('The target file is not valid UTF-8 text');
    const content = await pageWithoutLocation(page);
    expect(content).not.toContain(host.root);
    expect(content).not.toContain('/etc/hostname');
  });

  test('a target folder that leaves the deployment reads Outside allowed paths without a path', async ({
    page,
    request,
    service,
    host,
  }) => {
    const id = await createEnvironment(request, service, '.env.out', MANAGED);
    rmSync(host.file('environments'), { recursive: true, force: true });
    symlinkSync('/srv/deployments/sample-b', host.file('environments'));
    await page.goto(url(id));

    await expect(badge(page)).toHaveAttribute('data-status', 'blocked');
    await expect(badge(page)).toHaveText('Outside allowed paths');
    await expect(header(page)).toContainText('The target path leaves the deployment folder.');
    await expect(panel(page).locator('.bg-head')).toContainText('outside the allowed paths');
    await expect(overwriteButton(page)).toHaveCount(0);
    const content = await pageWithoutLocation(page);
    expect(content).not.toContain(host.root);
    expect(content).not.toContain('sample-b');
  });

  test('a certificate compares as one File content row', async ({
    page,
    request,
    service,
    host,
  }) => {
    const id = await createEnvironment(request, service, 'drift-ca.pem', PEM);
    await deploy(request, service, id);
    host.write('environments/drift-ca.pem', PEM_DISK);
    await page.goto(url(id));

    await expect(badge(page)).toHaveText('Drift');
    await expect(panel(page).locator('[data-fragment="drift-row"]')).toHaveCount(1);
    await expect(row(page, 'File content').locator('[data-change="disk-changed"]')).toBeVisible();
    await expectNoValueInPage(page, 'E2EDRIFTMANAGED', 'E2EDRIFTONDISK', 'BEGIN CERTIFICATE');
    await revealButton(page).click();
    await expect(row(page, 'File content').locator('[data-reveal-value]')).toHaveCount(3);
    await expect(page.locator('#drift-target-0 [data-reveal-value]')).toContainText(
      'E2EDRIFTONDISK',
    );
    await expect(page.locator('#drift-managed-0 [data-reveal-value]')).toContainText(
      'E2EDRIFTMANAGED',
    );
  });
});

test.describe('reveal', () => {
  test('answers only the values of the differing keys into their slots and hides on request', async ({
    page,
    drifted: id,
  }) => {
    await page.goto(url(id));
    await revealButton(page).click();

    await expect(panel(page)).toHaveAttribute('data-revealed', 'true');
    await expect(page.locator('#drift-target-0 [data-reveal-value]')).toHaveText(TARGET_VALUE);
    await expect(page.locator('#drift-deployed-0 [data-reveal-value]')).toHaveText(DEPLOYED_VALUE);
    await expect(page.locator('#drift-managed-0 [data-reveal-value]')).toHaveText(MANAGED_VALUE);
    await expect(page.locator('#drift-target-1 [data-reveal-value]')).toHaveText(ONLY_DISK_VALUE);
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(4);
    await expect(panel(page).locator('[data-reveal-value][hx-ignore]')).toHaveCount(4);
    expect(await page.content()).not.toContain(SAME_VALUE);
    await expect(page.getByRole('button', { name: 'Hide values' })).toBeVisible();

    await page.getByRole('button', { name: 'Hide values' }).click();
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, ...ALL_VALUES);
    await expectFits(page);
  });

  test('values go by themselves after 30 seconds', async ({ page, drifted: id }) => {
    await page.clock.install();
    await page.goto(url(id));
    await revealButton(page).click();
    await expect(page.locator('#drift-target-0 [data-reveal-value]')).toHaveText(TARGET_VALUE);
    await page.clock.fastForward(29_000);
    await expect(panel(page)).toHaveAttribute('data-revealed', 'true');
    await page.clock.fastForward(2_000);
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, ...ALL_VALUES);
  });

  test('a page that is out of date gets a notice and no value', async ({
    page,
    drifted: id,
    host,
  }) => {
    await page.goto(url(id));
    host.write('environments/.env.app', 'DRIFT_KEY=changed-after-the-page\n');
    await revealButton(page).click();

    await expect(notice(page)).toContainText(
      'The files changed since this page was opened. Reload the page to compare them again.',
    );
    await expect(panel(page)).toHaveAttribute('data-revealed', 'false');
    await expect(panel(page).locator('[data-reveal-value]')).toHaveCount(0);
    await expectNoValueInPage(page, ...ALL_VALUES, 'changed-after-the-page');
  });
});

test.describe('import', () => {
  test('saves the target as a new version, then the file deploys with no drift', async ({
    page,
    request,
    service,
    drifted: id,
    host,
  }) => {
    await page.goto(url(id));
    await importButton(page).click();

    await page.waitForURL(`**/environments/${id}/drift?imported=3`);
    await expect(toast(page)).toContainText('Imported the target as version 3. Pending deploy.');
    await expect(badge(page)).toHaveText('No drift');
    await expect(header(page)).toContainText('The target file matches the managed version.');
    await expect(importButton(page)).toHaveCount(0);
    expect(host.read('environments/.env.app')).toBe(TARGET);

    expect(await historyVersions(page, id)).toEqual(['v3', 'v2', 'v1']);
    await expect(
      page.locator('[data-fragment="version-row"]', { hasText: 'v3' }),
    ).toContainText('Imported from the target file');

    const response = await request.post(`/services/${service}/deploy`, {
      form: { selection: 'explicit', environment: String(id) },
      headers: await csrfHeaders(request),
    });
    const body = await response.text();
    expect(body).toContain('data-tone="ok"');
    expect(body).toContain('data-status="unchanged"');
    expect(host.list('environments')).toEqual(['.env.app']);
    expect(host.read('environments/.env.app')).toBe(TARGET);
  });

  test('is refused when the file changed after the page was shown', async ({
    page,
    drifted: id,
    host,
  }) => {
    await page.goto(url(id));
    host.write('environments/.env.app', `${TARGET}LATE=1\n`);
    await importButton(page).click();

    await expect(notice(page)).toContainText(
      'The target file changed since this page was opened. Reload the page.',
    );
    await expect(page).toHaveURL(new RegExp(`${url(id)}$`));
    expect(await historyVersions(page, id)).toEqual(['v2', 'v1']);
  });

  test('is refused when a newer version was saved after the page was shown', async ({
    page,
    request,
    drifted: id,
  }) => {
    await page.goto(url(id));
    await saveRaw(request, id, 2, `${MANAGED}NEWER=1\n`);
    await importButton(page).click();

    await expect(notice(page)).toContainText('This file changed since you opened it.');
    expect(await historyVersions(page, id)).toEqual(['v3', 'v2', 'v1']);
  });
});

test.describe('overwrite', () => {
  test('asks first, then replaces the target after keeping a 0600 backup', async ({
    page,
    drifted: id,
    host,
  }) => {
    await page.goto(url(id));
    const dialog = page.locator('#overwrite-dialog');
    await expect(dialog).toBeHidden();
    await overwriteButton(page).click();
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('h2')).toHaveText('Overwrite target file');
    await expect(dialog).toContainText('environments/.env.app');
    await expect(dialog).toContainText('managed version v2');
    await expect(dialog).toContainText('forced deploy of this file');
    await expect(dialog).toContainText('backup copy next to it');
    await expect(dialog).toContainText('audit log');

    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden();
    expect(host.read('environments/.env.app')).toBe(TARGET);
    expect(backups(host, '.env.app')).toEqual([]);

    await overwriteButton(page).click();
    await dialog.getByRole('button', { name: 'Overwrite target' }).click();
    await page.waitForURL(/drift\?overwritten=2&backup=1&same=0&post=ok$/);
    await expect(toast(page)).toContainText('Overwrote the target with version 2.');
    await expect(toast(page)).toContainText('The previous file was kept as a backup next to it.');
    await expect(badge(page)).toHaveText('No drift');
    await expect(overwriteButton(page)).toHaveCount(0);

    expect(host.read('environments/.env.app')).toBe(MANAGED);
    const [backup] = backups(host, '.env.app');
    expect(backups(host, '.env.app')).toHaveLength(1);
    expect(host.read(`environments/${backup}`)).toBe(TARGET);
    expect(statSync(host.file(`environments/${backup}`)).mode & 0o777).toBe(0o600);
    await expectFits(page);
  });

  test('the request is given a timeout far above the default of one minute', async ({
    page,
    drifted: id,
  }) => {
    await page.goto(url(id));
    // The page reloads after a successful overwrite, so the observation is kept in the session.
    await page.evaluate(() => {
      document.addEventListener('htmx:before:request', (event) => {
        const detail = (event as CustomEvent).detail as {
          ctx?: { sourceElement?: Element; request?: { timeout?: unknown } };
        };
        sessionStorage.setItem(
          'e2e-request',
          JSON.stringify({
            source: detail.ctx?.sourceElement?.id ?? '',
            timeout: Number(detail.ctx?.request?.timeout ?? 0),
          }),
        );
      });
    });
    await overwriteButton(page).click();
    await page
      .locator('#overwrite-dialog')
      .getByRole('button', { name: 'Overwrite target' })
      .click();
    await page.waitForURL(/overwritten=2/);

    const seen = await page.evaluate(
      () => JSON.parse(sessionStorage.getItem('e2e-request') ?? '{}') as Record<string, unknown>,
    );
    expect(seen.source).toBe('overwrite-form');
    expect(Number(seen.timeout)).toBeGreaterThan(3_600_000);
  });

  test('is refused in the dialog when a newer version was saved after the page was shown', async ({
    page,
    request,
    drifted: id,
    host,
  }) => {
    await page.goto(url(id));
    await saveRaw(request, id, 2, `${MANAGED}NEWER=1\n`);
    await overwriteButton(page).click();
    const dialog = page.locator('#overwrite-dialog');
    await dialog.getByRole('button', { name: 'Overwrite target' }).click();

    await expect(dialog.locator('[role="alert"]')).toContainText(
      'This file changed since you opened it.',
    );
    await expect(dialog).toBeVisible();
    expect(host.read('environments/.env.app')).toBe(TARGET);
    expect(backups(host, '.env.app')).toEqual([]);
  });

  test('deploys nothing without the confirmation the dialog posts', async ({
    request,
    drifted: id,
    host,
  }) => {
    const response = await request.post(`/environments/${id}/overwrite`, {
      form: { base_version: '2' },
      headers: await csrfHeaders(request),
    });
    expect(response.status()).toBe(422);
    expect(await response.text()).toContain('Confirm the overwrite to continue.');
    expect(host.read('environments/.env.app')).toBe(TARGET);
    expect(backups(host, '.env.app')).toEqual([]);
  });
});

test.describe('security policy', () => {
  test('the page, its reveal and its dialog raise no CSP violation and the page is not cached', async ({
    page,
    drifted: id,
  }) => {
    const messages: string[] = [];
    page.on('console', (message) => {
      if (/content security policy/i.test(message.text())) {
        messages.push(message.text());
      }
    });
    await page.addInitScript(() => {
      (window as unknown as { violations: string[] }).violations = [];
      document.addEventListener('securitypolicyviolation', (event) => {
        (window as unknown as { violations: string[] }).violations.push(
          `${event.violatedDirective} ${event.blockedURI}`,
        );
      });
    });

    const response = await page.goto(url(id));
    expect(response?.headers()['content-security-policy']).toContain("default-src 'self'");
    expect(response?.headers()['cache-control']).toBe('no-store');
    expect(response?.headers()['vary']).toContain('HX-Request');
    await revealButton(page).click();
    await expect(page.locator('#drift-target-0 [data-reveal-value]')).toHaveText(TARGET_VALUE);
    await overwriteButton(page).click();
    await expect(page.locator('#overwrite-dialog')).toBeVisible();
    expect(
      await page.evaluate(() => (window as unknown as { violations: string[] }).violations),
    ).toEqual([]);
    expect(messages).toEqual([]);
  });
});

test.describe('viewer', () => {
  test.use({ storageState: stateFile('viewer') });

  test('sees the masked comparison and no control, and the server refuses every action', async ({
    page,
    request,
    browser,
  }) => {
    const id = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
    const response = await page.goto(url(id));
    expect(response?.status()).toBe(200);
    await expectItemHeader(page, 'environment', '.env.db.app');
    await expect(badge(page)).toHaveText('Target missing');
    await expect(panel(page)).toContainText('Values are masked for the Viewer group.');
    await expect(page.getByRole('button', { name: /Reveal|Hide|Import|Overwrite/ })).toHaveCount(0);
    await expect(page.locator('#overwrite-dialog')).toHaveCount(0);
    await expect(header(page).getByRole('link', { name: 'Back to service' })).toBeVisible();
    const content = await pageWithoutLocation(page);
    expect(content).not.toContain('/opt/');
    expect(content).not.toContain('/srv/');

    const headers = await csrfHeaders(request);
    for (const [path, form] of [
      ['drift/reveal', { managed: '1' }],
      ['import', { base_version: '1' }],
      ['overwrite', { base_version: '1', confirm_overwrite: '1' }],
    ] as const) {
      const posted = await request.post(`/environments/${id}/${path}`, { form, headers });
      expect(posted.status(), path).toBe(403);
    }
  });
});

test.describe('scoped viewer', () => {
  test.use({ storageState: stateFile('envScopedViewer') });

  test('sees only its file, masked and path free, and is refused every action', async ({
    page,
    request,
    browser,
  }) => {
    const own = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
    const hidden = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
    const response = await page.goto(url(own));
    expect(response?.status()).toBe(200);
    await expect(page.getByRole('button', { name: /Reveal|Import|Overwrite/ })).toHaveCount(0);
    expect(await page.content()).not.toContain('/opt/');
    expect((await page.goto(url(hidden)))?.status()).toBe(404);

    const headers = await csrfHeaders(request);
    for (const path of ['drift/reveal', 'import', 'overwrite']) {
      expect((await request.post(`/environments/${own}/${path}`, { form: {}, headers })).status()).toBe(403);
      expect((await request.post(`/environments/${hidden}/${path}`, { form: {}, headers })).status()).toBe(404);
    }
  });
});

test.describe('admin', () => {
  test.use({ storageState: stateFile('admin') });

  test('gets the view and the server refuses the actions on a target it cannot reach', async ({
    page,
    request,
    browser,
  }) => {
    const id = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
    const response = await page.goto(url(id));
    expect(response?.status()).toBe(200);
    await expect(badge(page)).toHaveText('Target missing');
    await expect(importButton(page)).toHaveCount(0);
    await expect(overwriteButton(page)).toHaveCount(0);
    const before = await historyVersions(page, id);

    const headers = await csrfHeaders(request);
    const imported = await request.post(`/environments/${id}/import`, {
      form: { base_version: String(before.length), target_hash: '' },
      headers,
    });
    expect(imported.status()).toBe(422);
    const asked = await request.post(`/environments/${id}/overwrite`, {
      form: { base_version: String(before.length) },
      headers,
    });
    expect(asked.status()).toBe(422);
    const refused = await request.post(`/environments/${id}/overwrite`, {
      form: { base_version: String(before.length), confirm_overwrite: '1' },
      headers,
    });
    expect(refused.status()).toBe(422);
    expect(await refused.text()).toContain('The target folder does not exist.');
    expect(await historyVersions(page, id)).toEqual(before);
  });
});

test.describe('scoped admin', () => {
  test.use({ storageState: stateFile('envScopedAdmin') });

  test('sees the state of the file it holds without learning a path, and nothing else', async ({
    page,
    browser,
  }) => {
    const own = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db');
    const hidden = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
    expect((await page.goto(url(own)))?.status()).toBe(200);
    await expect(badge(page)).toHaveText('Target missing');
    await expect(header(page)).toContainText('The target folder does not exist.');
    expect(await page.content()).not.toContain('/opt/');
    expect((await page.goto(url(hidden)))?.status()).toBe(404);
  });
});

test.describe('admin without a grant', () => {
  test.use({ storageState: stateFile('ungrantedAdmin') });

  test('gets 404 for the view and for every action', async ({ page, request, browser }) => {
    const id = await seededEnvironmentId(browser, 'Fancy Proj. Name', 'prod', 'mysql', '.env.db.app');
    expect((await page.goto(url(id)))?.status()).toBe(404);
    const headers = await csrfHeaders(request);
    for (const path of ['drift/reveal', 'import', 'overwrite']) {
      const posted = await request.post(`/environments/${id}/${path}`, { form: {}, headers });
      expect(posted.status(), path).toBe(404);
    }
  });
});

