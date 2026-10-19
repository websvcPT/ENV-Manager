import { expect, type Locator } from '@playwright/test';

/**
 * Save changed post-deploy commands through the form. The first save only shows the arguments
 * each line was split into, the confirming save stores exactly those commands.
 */
export async function saveChangedCommands(form: Locator): Promise<void> {
  await form.getByRole('button', { name: 'Save settings' }).click();
  await expect(form.locator('#postdeploy-review')).toBeVisible();
  await form.getByRole('button', { name: 'Confirm and save' }).click();
}
