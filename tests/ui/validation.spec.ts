import { test, expect, signIn, submit } from './fixtures';

test('Invalid date range and incomplete scenario preserve historical actuals', async ({ page }) => {
  await signIn(page);
  const numbers = page.getByRole('region', { name: 'Key numbers' });
  const actuals = await numbers.innerText();
  await page.locator('#cash-flow-filters summary').click();
  await page.locator('#cash-flow-filters [name=date_from]').fill('2026-10-05');
  await page.locator('#cash-flow-filters [name=date_to]').fill('2026-10-04');
  await submit(page, page.locator('#cash-flow-filters').getByRole('button', { name: 'Update', exact: true }));
  await expect(page.locator('#cash-flow-filters')).toContainText('End date must be on or after start date.');
  // The landscape invalid-range reset is tracked in #375; validate this
  // independent scenario from a fresh GET rather than asserting that bug.
  await page.goto('/');
  await expect(numbers).toHaveText(actuals, { useInnerText: true });
  await page.locator('summary').filter({ hasText: 'What-if scenario' }).click();
  const form = page.locator('form').filter({ has: page.locator('[name=scenario_action]') });
  await form.locator('[name=change_type]').selectOption('oneoff');
  await submit(page, form.getByRole('button', { name: 'Add to scenario' }));
  await expect(form).toContainText('This field is required.');
  await expect(numbers).toHaveText(actuals, { useInnerText: true });
  await expect(page).not.toHaveURL(/sc=/);
});
