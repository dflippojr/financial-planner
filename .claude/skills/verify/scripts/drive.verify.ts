import { test, expect } from '@playwright/test';
import { signIn, submit, previewSyntheticCsv } from '../../../../tests/ui/fixtures';
import { selected, shot } from './features';

// The main path from SKILL.md's feature map, in order, on one disposable instance.
const main = (page: import('@playwright/test').Page) => page.locator('main');
test.beforeEach(async ({ page }) => { await signIn(page); });

if (selected('import')) test('import: synthetic CSV commits two new rows', async ({ page }) => {
  // Same upload and mapping as the contract's CSV journey, then commit instead of cancel.
  await page.goto('/accounts/');
  const href = await main(page).getByRole('link', { name: 'Import CSV', exact: true }).first().getAttribute('href');
  await page.goto(href!);
  await previewSyntheticCsv(page);
  await expect(main(page)).toContainText('2 new; 0 duplicate; 1 invalid.');
  await submit(page, page.getByRole('button', { name: 'Import new rows' }));
  await page.goto('/transactions/');
  await expect(page.locator('tr').filter({ hasText: 'Synthetic preview purchase' }).filter({ visible: true })).toContainText('12.34');
  await expect(page.locator('tr').filter({ hasText: 'Synthetic preview income' }).filter({ visible: true })).toContainText('56.78');
  await shot(page, 'desktop', 'import');
});

if (selected('cashflow')) test('cashflow: key numbers and by-period table render', async ({ page }) => {
  await page.goto('/');
  const numbers = page.getByRole('region', { name: 'Key numbers' });
  for (const label of ['Income', 'Spending', 'Net cash flow']) await expect(numbers).toContainText(label);
  await expect(main(page).getByRole('heading', { name: 'By period', exact: true })).toBeAttached();
  await shot(page, 'desktop', 'cashflow');
});

if (selected('recurring')) test('recurring: review page lists its sections', async ({ page }) => {
  await page.goto('/recurring/');
  await expect(page.getByRole('heading', { name: 'Recurring charges', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Confirmed', exact: true })).toBeVisible();
  await shot(page, 'desktop', 'recurring');
});

if (selected('dashboard')) test('dashboard: budgets and attention panels show the seeded budget', async ({ page }) => {
  await page.goto('/');
  await expect(main(page).getByRole('heading', { name: 'Budgets', exact: true }).filter({ visible: true })).toBeVisible();
  await expect(main(page)).toContainText('Dining');
  await expect(main(page).getByRole('heading', { name: 'Needs attention', exact: true }).filter({ visible: true })).toBeVisible();
  await shot(page, 'desktop', 'dashboard');
});

if (selected('alert')) test('alert: seeded Dining alert marks read and links to Budgets', async ({ page }) => {
  await page.goto('/alerts/');
  const row = page.getByRole('list', { name: 'Alerts' }).locator('li').filter({ hasText: 'Dining is over budget' });
  await expect(row).toContainText('Unread');
  await submit(page, row.getByRole('button', { name: 'Mark as read', exact: true }));
  await page.reload();
  await expect(row).not.toContainText('Unread');
  await expect(row.getByRole('button', { name: 'Mark as read', exact: true })).toHaveCount(0);
  await shot(page, 'desktop', 'alert');
  await row.getByRole('link', { name: 'Dining is over budget', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Budgets', exact: true })).toBeVisible();
});
