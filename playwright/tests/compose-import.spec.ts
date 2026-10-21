import { mkdirSync } from 'node:fs';

import type { APIRequestContext, Page } from '@playwright/test';

import { createDeployment, deploymentId } from '../deployments-api';
import { createHostFolder, type HostFolder } from '../host-files';
import { expectItemHeader, expectLocation } from '../item-header';
import { TOUCH_TARGET, horizontalOverflow, isWide } from '../viewport';
import { expect, test as scratchTest } from './scratch-project';

/*
 * Import from Docker Compose. Every spec roots a scratch deployment in a folder of its own below
 * the deployments folder, with a real Compose file, and never uses a seeded deployment. The
 * Compose file also names a file outside the folder, which is offered disabled and never read.
 */

const COMPOSE = `services:
  web:
    image: nginx
    env_file:
      - .env.web
      - .env.shared
    volumes:
      - ./conf/web.conf:/etc/web.conf:ro
  api:
    image: api
    env_file:
      - .env.api
      - .env.shared
      - ../outside.env
`;

const FILES: Record<string, string> = {
  'compose.yaml': COMPOSE,
  '.env.web': 'WEB=1\n',
  '.env.web.example': 'WEB=\n',
  '.env.api': 'API=1\n',
  '.env.shared': 'SHARED=1\n',
  'conf/web.conf': 'listen 80;\n',
};

const HAS_SERVICES = 'This deployment already has services, so nothing can be imported.';

/* A layout shaped like a real project: an env file with its dist file, an env file and a mounted
   file that are not on disk yet but have dist files, a missing data folder, two real folders and
   the deployment environment file beside the Compose file. */
const GEOTRAVEL_COMPOSE = `name: \${ENVIRONMENT_NAME:-geotravel}
services:
  db:
    image: mysql:8.0.41-debian
    env_file:
      - ./environments/.env.db
    volumes:
      - ./data/mysql/data:/var/lib/mysql
      - ./backups:/var/backups
      - ./scripts:/scripts
  api:
    image: api:\${IMAGE_API_VERSION:-latest}
    volumes:
      - ./environments/.env.api:/var/www/html/.env
  frontend:
    image: frontend:\${IMAGE_FRONTEND_VERSION:-latest}
    env_file:
      - ./environments/.env.frontend
`;

const GEOTRAVEL: Record<string, string> = {
  'docker-compose.yml': GEOTRAVEL_COMPOSE,
  '.env': 'ENVIRONMENT_NAME=tests\n',
  'environments/.env.db': 'MYSQL_USER=app\n',
  'environments/.env.db-dist': 'MYSQL_USER=\n',
  'environments/.env.frontend-dist': 'API_URL=http://localhost:8101\n',
  'environments/.env.api-dist': 'APP_KEY=\n',
  'backups/.keep': '',
  'scripts/init.sql': 'SELECT 1;\n',
};

const test = scratchTest.extend<{ host: HostFolder }>({
  host: async ({}, use) => {
    const host = createHostFolder();
    try {
      mkdirSync(host.file('conf'));
      for (const [relative, content] of Object.entries(FILES)) {
        host.write(relative, content);
      }
      await use(host);
    } finally {
      host.remove();
    }
  },
});

function prepare(request: APIRequestContext, projectId: number, host: HostFolder): Promise<number> {
  return createDeployment(request, projectId, 'compose-dep', host.root);
}

function fileRow(page: Page, key: string) {
  return page.locator(`[data-fragment="import-file-row"][data-file="${key}"]`);
}

function ownRow(page: Page, key: string) {
  return page.locator(`[data-fragment="import-deployment-file-row"][data-file="${key}"]`);
}

function serviceBox(page: Page, name: string) {
  return page.locator(
    `[data-fragment="import-service-row"][data-service="${name}"] input[name="service"]`,
  );
}

function toast(page: Page) {
  return page.locator('[data-fragment="toast"]');
}

async function scan(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Scan the files' }).click();
  await expect(page.locator('[data-fragment="import-preview"]')).toBeVisible();
}

test.describe('import from Docker Compose', () => {
  test('imports the services and files of a Compose project from the deployment page', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const id = await prepare(request, scratch.id, host);
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#services [data-fragment="empty-state"]')).toBeVisible();
    const open = page.locator('#import-compose-open');
    await expect(open).toHaveAttribute('href', `/deployments/${id}/import-compose`);
    await open.click();
    await expect(page).toHaveURL(new RegExp(`/deployments/${id}/import-compose$`));

    await expectItemHeader(page, 'deployment', 'compose-dep');
    await expectLocation(page, 'deployment', {
      parents: `Project: ${scratch.name}`,
      root: `Deployment path: ${host.root}`,
    });
    await expect(page.locator('nav[aria-label="Breadcrumb"] li')).toHaveText([
      'Projects',
      '/',
      scratch.name,
      '/',
      'compose-dep',
      '/',
      'Import from Docker Compose',
    ]);
    await expect(page.locator('input[name="compose_file"]')).toHaveCount(1);
    await expect(page.locator('input[name="compose_file"]')).toBeChecked();
    await expect(page.locator('#cf-extra')).toHaveValue('');

    await scan(page);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
    await expect(page.locator('[data-fragment="import-service-row"]')).toHaveCount(2);
    await expect(page.locator('[data-fragment="import-file-row"]')).toHaveCount(5);
    await expect(page.locator('[data-fragment="import-counts"]')).toContainText(
      '2 services and 6 files found (1 of the deployment itself), 1 file cannot be imported',
    );
    // The Compose file is offered ticked as a deployment file. Without a Docker socket no
    // post-deploy command is offered, and the preview says why.
    await expect(ownRow(page, 'compose.yaml').locator('input[name="deployment_file"]')).toBeChecked();
    await expect(page.locator('[data-fragment="import-no-commands"]')).toContainText(
      'The Docker socket is not mounted.',
    );
    await expect(page.locator('input[name^="run:"]')).toHaveCount(0);
    const outside = page.locator('[data-fragment="import-file-row"][data-file$="/outside.env"]');
    await expect(outside).toContainText('Outside the deployment folder or the allowed paths.');
    await expect(outside.locator('input[name="file"]')).toBeDisabled();
    await expect(outside.locator('input[name="file"]')).not.toBeChecked();
    const shared = fileRow(page, '.env.shared');
    const owner = shared.locator('select[name="owner:.env.shared"]');
    await expect(owner.locator('option')).toHaveText(['api', 'web']);
    await expect(
      fileRow(page, '.env.web').locator('input[name="dist:.env.web"]'),
    ).toHaveAttribute('value', '.env.web.example');
    if (!isWide(page)) {
      const box = await fileRow(page, '.env.web').locator('label').first().boundingBox();
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
      expect(box?.width ?? 0).toBeGreaterThanOrEqual(TOUCH_TARGET);
    }

    // Unticking a service unticks the files only it uses, and a shared file moves to the other.
    await owner.selectOption('web');
    await serviceBox(page, 'web').uncheck();
    await expect(fileRow(page, '.env.web').locator('input[name="file"]')).not.toBeChecked();
    await expect(fileRow(page, '.env.web').locator('input[name="file"]')).toBeDisabled();
    await expect(fileRow(page, 'conf/web.conf').locator('input[name="file"]')).not.toBeChecked();
    await expect(fileRow(page, '.env.api').locator('input[name="file"]')).toBeChecked();
    await expect(shared.locator('input[name="file"]')).toBeChecked();
    await expect(owner).toHaveValue('api');
    await serviceBox(page, 'web').check();
    await expect(fileRow(page, '.env.web').locator('input[name="file"]')).toBeChecked();
    await expect(fileRow(page, '.env.web').locator('input[name="file"]')).toBeEnabled();

    await fileRow(page, '.env.web').locator('input[name="dist:.env.web"]').check();
    await owner.selectOption('web');
    await page.getByRole('button', { name: 'Import selected…' }).click();

    const dialog = page.locator('#import-confirm');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('[data-import-count="services"]')).toHaveText('2 services');
    await expect(dialog.locator('[data-import-count="files"]')).toHaveText('5 files');
    await expect(dialog.locator('[data-import-count="content"]')).toContainText(
      '5 saved as version 1 as they are on disk, 0 created empty',
    );
    await expect(dialog.locator('[data-import-count="deployment-files"]')).toHaveText('1 file');
    await expect(dialog.locator('[data-import-count="commands"]')).toHaveText('0');
    await expect(dialog.locator('[data-import-count="linked"]')).toHaveText('1');
    await dialog.getByRole('button', { name: 'Import', exact: true }).click();

    await expect(page).toHaveURL(new RegExp(`/deployments/${id}\\?imported=2,5,5,0,1,0$`));
    await expect(toast(page)).toContainText(
      'Imported 2 services and 5 files (5 with content, 0 created empty). 1 item not imported.',
    );
    const rows = page.locator('#services [data-fragment="service-row"]');
    await expect(rows.locator('th a')).toHaveText(['api', 'web']);
    await expect(rows.filter({ hasText: 'web' }).locator('td').first()).toHaveText('3');
    await expect(page.locator('#import-compose-open')).toHaveCount(0);
    // Each service with files found on the host has one run in the history, labelled as an
    // import, and the files stay as they were.
    await expect(page.locator('#hist [data-fragment="history-row"]')).toHaveCount(3);
    await expect(page.locator('#hist [data-history-trigger]')).toHaveCount(3);
    await expect(page.locator('[data-fragment="deployment-files-row"]')).toContainText('1 file');
    expect(host.read('.env.web')).toBe(FILES['.env.web']);
    expect(host.read('.env.shared')).toBe(FILES['.env.shared']);

    await page.goto(`/deployments/${id}/import-compose`);
    await expect(page.locator('[data-fragment="import-files"]')).toContainText(HAS_SERVICES);
    await expect(page.locator('input[name="compose_file"]')).toHaveCount(0);
  });

  test('says why a scan fails and keeps the typed path', async ({ page, request, scratch, host }) => {
    const id = await prepare(request, scratch.id, host);
    await page.goto(`/deployments/${id}/import-compose`);
    await page.locator('#cf-extra').fill('missing.yaml');
    await page.getByRole('button', { name: 'Scan the files' }).click();
    const alert = page.locator('[data-fragment="import-files"] [role="alert"]');
    await expect(alert).toContainText('The Compose file missing.yaml does not exist.');
    await expect(page.locator('#cf-extra')).toHaveValue('missing.yaml');
    await expect(page.locator('input[name="compose_file"]')).toBeChecked();
    await page.locator('#cf-extra').fill('../outside.yaml');
    await page.getByRole('button', { name: 'Scan the files' }).click();
    await expect(alert).toContainText('A Compose file path must be relative to the deployment folder.');
  });

  test('refuses an import without a service and goes back to the first step', async ({
    page,
    request,
    scratch,
    host,
  }) => {
    const id = await prepare(request, scratch.id, host);
    await page.goto(`/deployments/${id}/import-compose`);
    await scan(page);
    await serviceBox(page, 'api').uncheck();
    await serviceBox(page, 'web').uncheck();
    await page.getByRole('button', { name: 'Import selected…' }).click();
    const dialog = page.locator('#import-confirm');
    await expect(dialog.locator('[role="alert"]')).toContainText('Review the selection');
    await expect(dialog).toContainText('Select at least one service.');
    await dialog.locator('[data-js="dialog-close"]').last().click();
    await expect(dialog).toBeHidden();
    await expect(page.locator('[data-fragment="import-preview"]')).toBeVisible();
    await page.getByRole('link', { name: 'Back', exact: true }).click();
    await expect(page.locator('input[name="compose_file"]')).toBeChecked();
  });

  test('is not offered on a deployment that has services', async ({ page, browser }) => {
    const id = await deploymentId(browser, 'Fancy Proj. Name', 'prod');
    await page.goto(`/deployments/${id}`);
    await expect(page.locator('#services')).toBeVisible();
    await expect(page.locator('#import-compose-open')).toHaveCount(0);
    await page.goto(`/deployments/${id}/import-compose`);
    await expect(page.locator('[data-fragment="import-files"]')).toContainText(HAS_SERVICES);
  });

  test('starts files that are not on disk from their dist file and offers the deployment files', async ({
    page,
    request,
    scratch,
  }) => {
    const geo = createHostFolder();
    try {
      mkdirSync(geo.file('backups'));
      mkdirSync(geo.file('scripts'));
      for (const [relative, content] of Object.entries(GEOTRAVEL)) {
        geo.write(relative, content);
      }
      const id = await createDeployment(request, scratch.id, 'geotravel', geo.root);
      await page.goto(`/deployments/${id}/import-compose`);
      await scan(page);
      await expect(page.locator('[data-fragment="import-file-row"]')).toHaveCount(3);
      const frontend = fileRow(page, 'environments/.env.frontend');
      await expect(frontend).toContainText('Not on disk');
      await expect(frontend.locator('input[name="file"]')).toBeChecked();
      const link = frontend.locator('input[name="dist:environments/.env.frontend"]');
      const start = frontend.locator('input[name="start:environments/.env.frontend"]');
      await expect(link).toBeChecked();
      await expect(start).toBeChecked();
      // Unlinking the dist file unticks the start, and starting from it links it again.
      await link.uncheck();
      await expect(start).not.toBeChecked();
      await start.check();
      await expect(link).toBeChecked();
      const api = fileRow(page, 'environments/.env.api');
      await expect(api).toContainText('Not on disk, looks like a file');
      await expect(api.locator('input[name="file"]')).toBeChecked();
      const ignored = page.locator('[data-fragment="import-ignored"]');
      await expect(ignored).toContainText('data/mysql/data');
      await expect(ignored).not.toContainText('.env');
      // The deployment .env and the Compose file are offered ticked as deployment files.
      await expect(page.locator('[data-fragment="import-file-row"][data-file=".env"]')).toHaveCount(0);
      await expect(page.locator('[data-fragment="import-deployment-file-row"]')).toHaveCount(2);
      const env = ownRow(page, '.env');
      await expect(env).toContainText('Deployment environment file');
      await expect(env.locator('input[name="deployment_file"]')).toBeChecked();
      await expect(
        ownRow(page, 'docker-compose.yml').locator('input[name="deployment_file"]'),
      ).toBeChecked();

      await page.getByRole('button', { name: 'Import selected…' }).click();
      const dialog = page.locator('#import-confirm');
      await expect(dialog.locator('[data-import-count="content"]')).toContainText(
        '3 saved as version 1 as they are on disk, 2 started from their dist file, 0 created empty',
      );
      await dialog.getByRole('button', { name: 'Import', exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`/deployments/${id}\\?imported=3,5,5,0,3,0$`));
      await expect(page.locator('[data-fragment="deployment-files-row"]')).toContainText('2 files');
      expect(geo.read('.env')).toBe(GEOTRAVEL['.env']);
      expect(geo.list('environments')).toEqual([
        '.env.api-dist',
        '.env.db',
        '.env.db-dist',
        '.env.frontend-dist',
      ]);
    } finally {
      geo.remove();
    }
  });
});
