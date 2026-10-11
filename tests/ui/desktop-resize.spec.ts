import { test, expect, signIn } from './fixtures';

test('Live resize preserves a partial budget dialog and functional row menus', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await signIn(page);
  await page.goto('/planning/budgets/');
  const trigger = page.getByRole('button', { name: 'Add budget', exact: true }).filter({ visible: true });
  await trigger.click();
  const dialog = page.locator('#add-budget-dialog');
  const amount = dialog.getByLabel('Monthly amount');
  const category = dialog.getByLabel('Category', { exact: true });
  await amount.fill('123.45');
  await category.selectOption({ label: 'Entertainment' });
  await amount.focus();
  const originalURL = page.url();
  for (const width of [1440, 768, 375, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(amount).toHaveValue('123.45');
    await expect(category.locator('option:checked')).toHaveText('Entertainment');
    await expect(amount).toBeFocused();
    await expect(dialog).toBeVisible();
    await expect(page).toHaveURL(originalURL);
    await expect(dialog.getByRole('button', { name: 'Add budget', exact: true })).toBeVisible();
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.click();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByLabel('Actions for Entertainment', { exact: true })).toHaveCount(0);
  const action = () => page.getByLabel('Actions for Dining', { exact: true }).filter({ visible: true });
  await action().click();
  await expect(action().locator('..').getByRole('link', { name: 'Edit', exact: true })).toBeVisible();
  await page.setViewportSize({ width: 375, height: 900 });
  await action().click();
  await expect(action().locator('..').getByRole('link', { name: 'Edit', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(action()).toBeFocused();
  await page.setViewportSize({ width: 1440, height: 900 });
  await action().click();
  await page.keyboard.press('Escape');
  await expect(action()).toBeFocused();
  expect(errors).toEqual([]);
});
