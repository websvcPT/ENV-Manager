import { deploymentId } from '../deployments-api';
import { actionControls } from '../tests/row-actions';
import { expect, test } from '../tests/scratch-project';
import { horizontalOverflow, isWide } from '../viewport';

/*
 * The New deployment page with a usable Docker, on the demo stack: the stack is found by Docker
 * and in its folder, and is linked to the seeded demo deployment that already uses it instead of
 * offering Use. The workloads are started by the specs that deploy before this one (they run in
 * file order), so Docker lists the project. Run by `make e2e-demo`.
 */

const DEMO_ROOT = process.env.E2E_DEMO_ROOT ?? '';
const STACK = `${DEMO_ROOT}/stack`;
const DEMO_PROJECT = 'env-manager-demo';

test.describe('new deployment with a usable Docker', () => {
  test('finds the demo stack and links it to the deployment that uses it', async ({
    page,
    browser,
    scratch,
  }) => {
    expect(DEMO_ROOT, 'E2E_DEMO_ROOT must name the demo folder of the host').not.toBe('');
    const used = await deploymentId(browser, 'demo', 'stack');

    await page.goto(`/projects/${scratch.id}/deployments/new`);
    await expect(page.locator('#detected-stacks')).toHaveAttribute('aria-busy', 'false');
    await expect(page.locator('#detected-stacks')).not.toContainText('Docker was not used');

    const entry = page.locator(`[data-folder="${STACK}"]:visible`);
    await expect(entry).toContainText(DEMO_PROJECT);
    await expect(entry).toContainText('running');
    await expect(entry).toContainText(
      isWide(page) ? 'Docker and folder' : 'Found by docker and folder',
    );
    await expect(entry).toContainText('compose.yaml');
    const link = entry.locator('[data-stack-used]');
    await expect(link).toHaveAttribute('href', `/deployments/${used}`);
    await expect(link).toHaveText('Used by stack');
    await expect(actionControls(entry, 'use')).toHaveCount(0);
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);

    await link.click();
    await expect(page).toHaveURL(`/deployments/${used}`);
  });
});
