import { test, expect, signIn, submit } from './fixtures';

test('Desktop chat Delete all confirms and removes persisted conversation history', async ({ page }) => {
  await signIn(page);
  await page.goto('/chat/');
  await page.getByLabel('Question', { exact: true }).filter({ visible: true }).fill('Synthetic conversation to delete');
  await submit(page, page.getByRole('button', { name: 'Send', exact: true }).filter({ visible: true }));
  await expect(page.getByText('Synthetic UI reply. No real provider was contacted.', { exact: true }).filter({ visible: true })).toBeVisible({ timeout: 20_000 });
  await page.reload();
  await expect(page.locator('main')).toContainText('Synthetic conversation to delete');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('main form[action="/chat/delete-all/"] button').filter({ visible: true }).click();
  await page.reload();
  await expect(page.locator('main')).toContainText('Synthetic conversation to delete');
  page.once('dialog', dialog => dialog.accept());
  await submit(page, page.locator('main form[action="/chat/delete-all/"] button').filter({ visible: true }));
  await page.reload();
  await expect(page.locator('main')).not.toContainText('Synthetic conversation to delete');
  await expect(page.getByRole('navigation', { name: 'Conversations', exact: true }).locator('li')).toHaveCount(0);
});
