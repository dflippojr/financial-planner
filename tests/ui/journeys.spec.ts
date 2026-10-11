import { test, expect, signIn, openShellCorrection, audit, submit } from './fixtures';
import type { Page } from '@playwright/test';

const main = (page: Page) => page.locator('main');
test.beforeEach(async ({ page }) => { await signIn(page); });

test('Home range and URL-only scenario retain state without changing actuals', async ({ page }) => {
  const actuals = await page.getByRole('region', { name: 'Key numbers' }).innerText();
  await page.locator('#cash-flow-filters summary').click();
  const rangeToday = new Date();
  const rangeEnd = [rangeToday.getFullYear(), String(rangeToday.getMonth() + 1).padStart(2, '0'), String(rangeToday.getDate()).padStart(2, '0')].join('-');
  const rangeStart = rangeEnd.slice(0, 8) + '01';
  await page.locator('#cash-flow-filters [name=date_from]').fill(rangeStart);
  await page.locator('#cash-flow-filters [name=date_to]').fill(rangeEnd);
  await page.locator('#cash-flow-filters [name=grouping]').selectOption('week');
  await submit(page, page.locator('#cash-flow-filters').getByRole('button', { name: 'Update', exact: true }));
  await expect(page).toHaveURL(/grouping=week/);
  await page.reload();
  await page.locator('#cash-flow-filters summary').click();
  await expect(page.locator('#cash-flow-filters [name=grouping]')).toHaveValue('week');
  await expect(page.locator('#cash-flow-filters [name=date_from]')).toHaveValue(rangeStart);
  await expect(page.locator('#cash-flow-filters [name=date_to]')).toHaveValue(rangeEnd);
  await page.getByRole('link', { name: 'Reset range', exact: true }).click();
  await page.locator('summary').filter({ hasText: 'What-if scenario' }).click();
  const form = page.locator('form').filter({ has: page.locator('[name=scenario_action]') });
  await form.locator('[name=change_type]').selectOption('oneoff');
  await form.locator('[name=name]').fill('Synthetic scenario expense');
  await form.locator('[name=kind]').selectOption('expense');
  await form.locator('[name=amount]').fill('10.00');
  const now = new Date();
  const month = new Date(now.getFullYear(), now.getMonth() + 1, 1);
  await form.locator('[name=start_date]').fill([month.getFullYear(), String(month.getMonth() + 1).padStart(2, '0'), '01'].join('-'));
  await submit(page, form.getByRole('button', { name: 'Add to scenario' }));
  await expect(page).toHaveURL(/sc=/);
  await page.reload();
  await expect(main(page)).toContainText('Synthetic scenario expense');
  await expect(page.getByRole('region', { name: 'Key numbers' })).toHaveText(actuals, { useInnerText: true });
  await page.getByRole('link', { name: 'Remove', exact: true }).click();
  await expect(main(page)).not.toContainText('Synthetic scenario expense');
});

test('Category and description corrections persist with synthetic actor history', async ({ page }) => {
  const edit = await openShellCorrection(page);
  const id = edit.split('/')[2];
  const category = page.locator('form[action$="/category/"]');
  await category.getByLabel('Category', { exact: true }).selectOption({ label: 'Transportation' });
  await submit(page, category.getByRole('button', { name: 'Save category', exact: true }));
  await page.goto(edit);
  await expect(page.locator('form[action$="/category/"] option:checked')).toHaveText('Transportation');
  await expect(page.locator('ol li').filter({ hasText: 'Category:' })).toContainText('Synthetic Alex');
  await page.getByLabel('Description', { exact: true }).fill('Synthetic corrected Shell');
  await submit(page, page.getByRole('button', { name: 'Save correction' }));
  await page.goto(edit);
  await expect(page.getByLabel('Description', { exact: true })).toHaveValue('Synthetic corrected Shell');
  await expect(page.locator('ol li').filter({ hasText: 'Description:' })).toContainText('Synthetic Alex');
  await page.getByLabel('Description', { exact: true }).fill('Unsaved synthetic description');
  await main(page).getByRole('link', { name: 'Cancel', exact: true }).click();
  await expect(page.locator('tr').filter({ hasText: 'Synthetic corrected Shell' })).toContainText('Transportation');
  await audit(page, 'transaction_corrected', 'transaction', id);
});

test('Note and tag add/remove persist; tag membership has an audit event', async ({ page }) => {
  const edit = await openShellCorrection(page);
  await page.getByLabel('Note', { exact: true }).fill('Synthetic UI note');
  await page.getByLabel('Create tag', { exact: true }).fill('synthetic-ui');
  await submit(page, page.getByRole('button', { name: 'Save note and tags' }));
  await page.goto(edit);
  await expect(page.getByLabel('Note', { exact: true })).toHaveValue('Synthetic UI note');
  await expect(page.getByLabel('synthetic-ui', { exact: true })).toBeChecked();
  await audit(page, 'tags_changed', 'transaction', edit.split('/')[2]);
  await page.goto(edit);
  await page.getByLabel('Note', { exact: true }).fill('');
  await page.getByLabel('synthetic-ui', { exact: true }).uncheck();
  await submit(page, page.getByRole('button', { name: 'Save note and tags' }));
  await page.goto(edit);
  await expect(page.getByLabel('Note', { exact: true })).toHaveValue('');
  await expect(page.getByLabel('synthetic-ui', { exact: true })).not.toBeChecked();
  await audit(page, 'tags_changed', 'transaction', edit.split('/')[2]);
  await expect(page.locator('tbody tr').filter({ hasText: 'transaction #' + edit.split('/')[2] }).filter({ hasText: 'Member #1' })).toHaveCount(2);
  // Ordinary note edits deliberately do not create audit events (#98).
});

test('Split validates total, persists exact parts, and unsplits', async ({ page }) => {
  const edit = await openShellCorrection(page);
  const form = page.locator('#split-form');
  await form.getByLabel('Part 1 category').selectOption({ label: 'Transportation' });
  await form.getByLabel('Part 2 category').selectOption({ label: 'Dining' });
  await form.getByLabel('Part 1 amount').fill('-20.00');
  await form.getByLabel('Part 2 amount').fill('-20.00');
  await submit(page, form.getByRole('button', { name: 'Save split' }));
  await expect(page.getByRole('heading', { name: 'Split parts', exact: true })).toHaveCount(0);
  await expect(main(page).getByRole('alert')).toBeVisible();
  await form.getByLabel('Part 2 amount').fill('-21.18');
  await submit(page, form.getByRole('button', { name: 'Save split' }));
  await page.goto(edit);
  await expect(page.getByRole('heading', { name: 'Split parts', exact: true })).toBeVisible();
  await expect(page.locator('ol li').filter({ hasText: 'Category:' })).toContainText('Transportation $20.00, Dining $21.18');
  await audit(page, 'transaction_corrected', 'transaction', edit.split('/')[2]);
  await page.goto(edit);
  const unsplit = page.locator('form[action$="/unsplit/"]');
  await unsplit.getByLabel('Category', { exact: true }).selectOption({ label: 'Transportation' });
  await submit(page, unsplit.getByRole('button', { name: 'Unsplit', exact: true }));
  await page.goto(edit);
  await expect(page.getByRole('heading', { name: 'Split parts', exact: true })).toHaveCount(0);
  await expect(page.locator('form[action$="/category/"] option:checked')).toHaveText('Transportation');
  await audit(page, 'transaction_corrected', 'transaction', edit.split('/')[2]);
  await expect(page.locator('tbody tr').filter({ hasText: 'transaction #' + edit.split('/')[2] }).filter({ hasText: 'Member #1' })).toHaveCount(2);
});

test('Receipt rejects text, then upload/download/remove persist and are audited', async ({ page }) => {
  const edit = await openShellCorrection(page);
  await page.getByLabel('Receipt photo or PDF').setInputFiles({ name: 'synthetic.txt', mimeType: 'text/plain', buffer: Buffer.from('synthetic invalid receipt') });
  await submit(page, page.getByRole('button', { name: 'Attach receipt' }));
  await expect(main(page).getByRole('alert')).toBeVisible();
  await page.getByLabel('Receipt photo or PDF').setInputFiles('tests/fixtures/ui-pass/synthetic-receipt.png');
  await submit(page, page.getByRole('button', { name: 'Attach receipt' }));
  await page.goto(edit);
  const link = page.getByRole('link', { name: 'synthetic-receipt.png', exact: true });
  await expect(link).toBeVisible();
  const response = await page.request.get((await link.getAttribute('href'))!);
  expect(response.status()).toBe(200);
  expect(response.headers()['content-type']).toBe('image/png');
  const receiptID = (await link.getAttribute('href'))!.split('/')[4];
  await audit(page, 'receipt_attached', 'receipt', receiptID);
  await page.goto(edit);
  await submit(page, link.locator('..').getByRole('button', { name: 'Remove', exact: true }));
  await page.goto(edit);
  await expect(link).toHaveCount(0);
  await audit(page, 'receipt_removed', 'receipt', receiptID);
});

test('Budget edit validates zero, persists value and cancel leaves it unchanged', async ({ page }) => {
  await page.goto('/planning/budgets/');
  const menu = page.getByLabel('Actions for Dining', { exact: true }).filter({ visible: true });
  await menu.click();
  await menu.locator('..').getByRole('link', { name: 'Edit', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Edit budget', exact: true })).toBeVisible();
  const edit = new URL(page.url()).pathname;
  await page.getByLabel('Monthly amount').fill('0');
  await submit(page, page.getByRole('button', { name: 'Save', exact: true }));
  await expect(main(page)).toContainText('Ensure this value is greater than or equal to 0.01');
  await page.getByLabel('Monthly amount').fill('550.00');
  await submit(page, page.getByRole('button', { name: 'Save', exact: true }));
  await page.goto(edit);
  await expect(page.getByLabel('Monthly amount')).toHaveValue('550');
  await page.getByLabel('Monthly amount').fill('999');
  await main(page).getByRole('link', { name: 'Cancel', exact: true }).click();
  await page.goto(edit);
  await expect(page.getByLabel('Monthly amount')).toHaveValue('550');
  await audit(page, 'record_edited', 'budget', edit.split('/')[3]);
  await page.goto('/planning/budgets/');
  await page.getByRole('button', { name: 'Add budget', exact: true }).filter({ visible: true }).click();
  const dialog = page.locator('#add-budget-dialog');
  await dialog.getByLabel('Category', { exact: true }).selectOption({ label: 'Entertainment' });
  await dialog.getByLabel('Monthly amount').fill('100.00');
  await submit(page, dialog.getByRole('button', { name: 'Add budget', exact: true }));
  await page.reload();
  const created = page.getByLabel('Actions for Entertainment', { exact: true }).filter({ visible: true });
  await created.click();
  const budgetID = (await created.locator('..').getByRole('link', { name: 'Edit', exact: true }).getAttribute('href'))!.split('/')[3];
  await audit(page, 'record_created', 'budget', budgetID);
  await page.goto('/planning/budgets/');
  await created.click();
  page.once('dialog', dialog => dialog.accept());
  await submit(page, created.locator('..').getByRole('button', { name: 'Archive', exact: true }));
  await page.reload();
  await expect(page.getByLabel('Actions for Entertainment', { exact: true })).toHaveCount(0);
  await audit(page, 'record_archived', 'budget', budgetID);

});

test('Alert read markers and settings persist after reload', async ({ page }) => {
  await page.goto('/alerts/');
  const title = await main(page).locator('li').filter({ has: page.getByRole('button', { name: 'Mark as read', exact: true }) }).first().locator('a').innerText();
  const row = main(page).locator('li').filter({ hasText: title });
  await submit(page, row.getByRole('button', { name: 'Mark as read', exact: true }));
  await page.reload();
  await expect(row.getByRole('button', { name: 'Mark as read', exact: true })).toHaveCount(0);
  await submit(page, page.getByRole('button', { name: 'Mark all as read', exact: true }));
  await page.reload();
  await expect(main(page).getByRole('button', { name: 'Mark as read', exact: true })).toHaveCount(0);
  await page.goto('/settings/alerts/');
  const toggle = page.getByLabel('Budgets near or over the limit', { exact: true });
  const before = await toggle.isChecked();
  await toggle.setChecked(!before);
  await submit(page, page.getByRole('button', { name: 'Save alert settings', exact: true }));
  await page.reload();
  expect(await toggle.isChecked()).toBe(!before);
  await audit(page, 'notification_preferences_changed', 'member', '1');
});

test('Fake-provider chat history persists and a new conversation has a blank composer', async ({ page }) => {
  await page.goto('/chat/');
  const question = page.getByLabel('Question', { exact: true }).filter({ visible: true });
  await question.fill('How much is my synthetic spending?');
  await submit(page, page.getByRole('button', { name: 'Send', exact: true }).filter({ visible: true }));
  const reply = page.getByText('Synthetic UI reply. No real provider was contacted.', { exact: true }).filter({ visible: true });
  await expect(reply).toBeVisible({ timeout: 20_000 });
  const conversation = new URL(page.url()).searchParams.get('c');
  expect(conversation).toMatch(/^\d+$/);
  await page.reload();
  await expect(reply).toBeVisible();
  await expect(main(page)).toContainText('How much is my synthetic spending?');
  await submit(page, page.getByRole('button', { name: 'New conversation', exact: true }).filter({ visible: true }));
  await expect(question).toHaveValue('');
  const conversations = page.getByRole('navigation', { name: 'Conversations', exact: true });
  const summary = conversations.locator('summary');
  if (await summary.isVisible()) await summary.click();
  const old = conversations.locator('li').filter({ has: page.locator('a[href$="?c=' + conversation + '"]') });
  await submit(page, old.getByRole('button', { name: 'Delete conversation', exact: true }));
  await page.reload();
  await expect(conversations.locator('a[href$="?c=' + conversation + '"]')).toHaveCount(0);
  // Phone Delete all is covered by defect #371; desktop confirmation is tested separately.

});

test('Generic CSV preview shows exact signs and invalid row, cancel deletes stage', async ({ page }) => {
  await page.goto('/accounts/');
  // Import hub account choice resolves the visible account rather than relying on a database ID.
  const href = await main(page).getByRole('link', { name: 'Import CSV', exact: true }).first().getAttribute('href');
  await page.goto(href!);
  const importURL = new URL(page.url()).pathname;
  await page.getByLabel('Import profile').selectOption('generic');
  await page.getByLabel('CSV, OFX or QFX file').setInputFiles('tests/fixtures/ui-pass/synthetic.csv');
  await submit(page, page.getByRole('button', { name: 'Upload and map' }));
  for (const [name, value] of [['date_column', 'Date'], ['description_column', 'Description'], ['amount_column', 'Amount'], ['currency_column', 'Currency']]) await page.locator('[name=' + name + ']').selectOption(value);
  await page.locator('[name=date_format]').selectOption('iso');
  await page.locator('[name=number_format]').selectOption('dot_none');
  await page.getByLabel('One signed amount column').check();
  await submit(page, page.getByRole('button', { name: 'Preview rows' }));
  await expect(page.getByRole('heading', { name: 'Preview results' })).toBeVisible();
  await expect(main(page)).toContainText('2 new; 0 duplicate; 1 invalid.');
  await expect(page.locator('tr').filter({ hasText: 'Synthetic preview purchase' })).toContainText('12.34');
  await expect(page.locator('tr').filter({ hasText: 'Synthetic preview income' })).toContainText('56.78');
  await page.reload();
  await expect(page.getByRole('button', { name: 'Cancel and delete upload' })).toBeVisible();
  await submit(page, page.getByRole('button', { name: 'Cancel and delete upload' }));
  await expect(page.getByRole('heading', { name: 'Cash flow', exact: true })).toBeVisible();
  await page.goto(importURL);
  await expect(page.getByRole('button', { name: 'Upload and map' })).toBeVisible();
  await page.goto('/transactions/');
  await expect(main(page)).not.toContainText('Synthetic preview purchase');
  await expect(main(page)).not.toContainText('Synthetic preview income');
});

test('Settings tabs are reachable and sign-out revokes the current session', async ({ page }) => {
  for (const path of ['security', 'categories', 'tags', 'alerts', 'data', 'ai', 'audit', 'household']) {
    const response = await page.goto('/settings/' + path + '/');
    expect(response!.status()).toBe(200);
    await expect(main(page).getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();
  }
  await page.goto('/more/');
  const theme = page.getByRole('button', { name: /^(Dark theme|Switch to (dark|light) theme)$/ }).filter({ visible: true });
  const original = await page.locator('html').getAttribute('data-theme');
  await theme.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', original === 'light' ? 'dark' : 'light');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', original === 'light' ? 'dark' : 'light');
  await submit(page, page.getByRole('button', { name: 'Sign out', exact: true }).filter({ visible: true }));
  await page.goto('/transactions/');
  await expect(page).toHaveURL(/\/sign-in\//);
});
