import { test, expect, submit } from './fixtures';

test('Invalid sign-in is rejected and authenticated deep link retains its destination', async ({ page }) => {
  await page.goto('/transactions/');
  await expect(page).toHaveURL(/\/sign-in\//);
  await page.getByLabel('Username', { exact: true }).fill('synthetic_alex');
  await page.getByLabel('Password', { exact: true }).fill('Synthetic-invalid-password!');
  await submit(page, page.getByRole('button', { name: 'Sign in', exact: true }));
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.locator('main').getByRole('alert')).toBeVisible();
  await page.getByLabel('Password', { exact: true }).fill('Synthetic-passphrase-42!');
  await submit(page, page.getByRole('button', { name: 'Sign in', exact: true }));
  await expect(page).toHaveURL(/\/transactions\/$/);
  await expect(page.getByRole('heading', { name: 'Transactions', exact: true })).toBeVisible();
});
