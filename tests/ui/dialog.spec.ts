import { test, expect, signIn } from './fixtures';

test('Keyboard dialog retains input through rotation and restores focus on Escape', async ({ page }) => {
  await signIn(page);
  await page.goto('/planning/budgets/');
  const original = page.viewportSize()!;
  const trigger = page.getByRole('button', { name: 'Add budget', exact: true }).filter({ visible: true });
  await trigger.focus();
  await page.keyboard.press('Space');
  const dialog = page.locator('#add-budget-dialog');
  await expect(dialog).toBeVisible();
  const amount = dialog.getByLabel('Monthly amount');
  await amount.fill('123.45');
  await amount.focus();
  const url = page.url();
  for (const size of [
    { width: original.width, height: Math.round(original.height * 0.6) },
    { width: original.height, height: original.width }, original,
  ]) {
    await page.setViewportSize(size);
    await expect(amount).toHaveValue('123.45');
    await expect(amount).toBeFocused();
    await expect(page).toHaveURL(url);
    await dialog.getByRole('button', { name: 'Add budget', exact: true }).scrollIntoViewIfNeeded();
    await expect(dialog.getByRole('button', { name: 'Add budget', exact: true })).toBeInViewport();
  }
  for (let step = 0; step < 12; step++) {
    await page.keyboard.press('Tab');
    // Native dialog tab wrap may momentarily focus body before its first control.
    if (await page.evaluate(() => document.activeElement === document.body)) await page.keyboard.press('Tab');
    expect(await page.evaluate(() => document.activeElement?.closest('#add-budget-dialog') !== null)).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});
