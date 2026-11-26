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

if (selected('transfers')) test('transfers: undo keeps the row in place, restore brings the pair back', async ({ page }) => {
  // The seed's "Transfer to Savings" (-500.00) has no other leg; add a savings account and its deposit.
  await page.goto('/accounts/');
  const add = page.locator('#add-account');
  await add.locator('[name=name]').fill('Synthetic Savings');
  await add.locator('[name=account_type]').selectOption('savings');
  await submit(page, add.getByRole('button', { name: 'Add account', exact: true }));
  await page.goto('/transactions/add/');
  const savings = await page.locator('[name=account] option').filter({ hasText: 'Synthetic Savings' }).getAttribute('value');
  await page.locator('[name=account]').selectOption(savings!);
  const today = new Date();
  const local = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  await page.locator('[name=transaction_date]').fill(local);
  await page.locator('[name=direction]').selectOption('in');
  await page.locator('[name=amount]').fill('500.00');
  await page.locator('[name=description]').fill('Transfer from Checking');
  await submit(page, page.getByRole('button', { name: 'Add transaction', exact: true }));

  await page.goto('/transfers/');
  const excluded = page.locator('section[aria-labelledby=transfer-excluded] tbody tr');
  const row = excluded.filter({ hasText: 'Synthetic Savings' });
  await expect(row.getByRole('button', { name: 'Undo exclusion', exact: true })).toBeVisible();
  const position = await excluded.evaluateAll((rows, name) => rows.findIndex(r => r.textContent!.includes(name)), 'Synthetic Savings');
  await shot(page, 'desktop', 'transfers-1-excluded');

  // Undo: the same row stays where it was, marked Undone, with Restore where Undo was.
  await submit(page, row.getByRole('button', { name: 'Undo exclusion', exact: true }));
  await expect(row).toContainText('Undone');
  await expect(row.getByRole('button', { name: 'Restore', exact: true })).toBeVisible();
  expect(await excluded.evaluateAll((rows, name) => rows.findIndex(r => r.textContent!.includes(name)), 'Synthetic Savings')).toBe(position);
  const recent = page.locator('section[aria-labelledby=transfer-recently-undone]');
  await expect(recent.locator('tbody tr').filter({ hasText: 'Synthetic Savings' })).toHaveCount(1);
  await shot(page, 'desktop', 'transfers-2-undone-in-place');

  // Restore from the same spot: the pair is excluded again, still in its row.
  await submit(page, row.getByRole('button', { name: 'Restore', exact: true }));
  await expect(row.getByRole('button', { name: 'Undo exclusion', exact: true })).toBeVisible();
  await expect(row).not.toContainText('Undone');
  expect(await excluded.evaluateAll((rows, name) => rows.findIndex(r => r.textContent!.includes(name)), 'Synthetic Savings')).toBe(position);
  await shot(page, 'desktop', 'transfers-3-restored-in-place');

  // Undo again, then a fresh visit: the in-place row is gone and Recently undone offers Restore.
  await submit(page, row.getByRole('button', { name: 'Undo exclusion', exact: true }));
  await page.goto('/transfers/');
  await expect(excluded.filter({ hasText: 'Synthetic Savings' })).toHaveCount(0);
  await recent.scrollIntoViewIfNeeded();
  await shot(page, 'desktop', 'transfers-4-recently-undone');
  await submit(page, recent.locator('tbody tr').filter({ hasText: 'Synthetic Savings' }).getByRole('button', { name: 'Restore', exact: true }));

  // Restored: an ordinary excluded row again, and Recently undone no longer lists it.
  await expect(row.getByRole('button', { name: 'Undo exclusion', exact: true })).toBeVisible();
  await expect(row).not.toContainText('Undone');
  await expect(recent).toContainText('No pairs undone');
  await shot(page, 'desktop', 'transfers-5-restored');
});
