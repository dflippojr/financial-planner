import { test, expect, signIn, submit, noOverflow } from './fixtures';

// Runs on one WebKit and one Chromium forms project (see playwright.config.ts); the other
// forms projects get the same overflow checks from breakpoints.spec.ts.
test('Long settings and CSV choice labels reflow with contained AI table (#365)', async ({ page }) => {
  await signIn(page);
  // The 320/375/768/1440 overflow checks on these settings pages are owned by breakpoints.spec.ts.
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const path of ['/settings/alerts/', '/settings/ai/']) {
    await page.goto(path);
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    await noOverflow(page, 1280);
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
  }
  await page.goto('/accounts/');
  const href = await page.locator('main').getByRole('link', { name: 'Import CSV', exact: true }).first().getAttribute('href');
  await page.goto(href!);
  await page.getByLabel('Import profile').selectOption('generic');
  await page.getByLabel('CSV, OFX or QFX file').setInputFiles('tests/fixtures/ui-pass/synthetic.csv');
  await submit(page, page.getByRole('button', { name: 'Upload and map' }));
  for (const width of [320, 375, 412, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await noOverflow(page, width);
    const defaultChoice = page.getByLabel('Use as the default mapping for this account', { exact: true });
    const label = defaultChoice.locator('..');
    const before = await defaultChoice.isChecked();
    await label.click();
    expect(await defaultChoice.isChecked()).toBe(!before);
    await label.click();
    expect(await defaultChoice.isChecked()).toBe(before);
    for (const name of ['One signed amount column', 'Separate debit and credit columns']) {
      const choice = page.getByLabel(name, { exact: true });
      await choice.locator('..').click();
      await expect(choice).toBeChecked();
    }
  }
  for (const [name, value] of [['date_column', 'Date'], ['description_column', 'Description'], ['amount_column', 'Amount'], ['currency_column', 'Currency']]) await page.locator('[name=' + name + ']').selectOption(value);
  await page.locator('[name=date_format]').selectOption('iso');
  await page.locator('[name=number_format]').selectOption('dot_none');
  await page.getByLabel('One signed amount column').check();
  await page.setViewportSize({ width: 412, height: 915 });
  await submit(page, page.getByRole('button', { name: 'Preview rows' }));
  await expect(page.getByRole('heading', { name: 'Preview results' })).toBeVisible();
  await expect(page.locator('main')).toContainText('2 new; 0 duplicate; 1 invalid.');
  await submit(page, page.getByRole('button', { name: 'Cancel and delete upload' }));
});
