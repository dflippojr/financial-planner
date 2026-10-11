import { test, expect, signIn, openShellCorrection, submit } from './fixtures';
import type { Page } from '@playwright/test';

async function noOverflow(page: Page, width: number) {
  await expect.poll(() => page.evaluate(() => innerWidth)).toBe(width);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

async function fieldGeometry(page: Page) {
  const fields = page.locator('main .input, main .select, main .textarea, main .file-input').filter({ visible: true });
  expect(await fields.count()).toBeGreaterThan(0);
  for (const field of await fields.all()) {
    const box = await field.boundingBox();
    expect(Math.round(box!.height * 100) / 100).toBeGreaterThanOrEqual(44);
    expect(Math.round(box!.width * 100) / 100).toBeGreaterThanOrEqual(44);
    await field.focus();
    expect(await field.evaluate(el => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);
  }
  const regions = await page.locator('main .form-field').filter({ visible: true }).evaluateAll(fields => fields.map(field => {
    const control = field.querySelector('input:not([type=hidden]),select,textarea');
    const target = control?.closest('label') || control;
    const box = target?.getBoundingClientRect();
    return box ? { top: box.top, bottom: box.bottom, left: box.left, right: box.right } : null;
  }).filter(Boolean));
  for (let i = 1; i < regions.length; i++) {
    const before = regions[i - 1]!;
    const after = regions[i]!;
    if (after.top >= before.bottom && after.left < before.right && after.right > before.left) {
      expect(after.top - before.bottom).toBeGreaterThanOrEqual(8);
    }
  }
}

async function focusVisible(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const active = document.activeElement!;
    const target = active.closest('.form-check-label, .form-choices label') || active;
    const box = target.getBoundingClientRect();
    const dock = document.querySelector('.dock')!.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    const visible = box.top >= 4 && box.bottom <= dock.top - 4 && !!hit && target.contains(hit);
    return visible ? 'visible' : JSON.stringify({ path: location.pathname, id: active.id, tag: active.tagName, top: box.top, bottom: box.bottom, dock: dock.top, hit: hit?.tagName, viewport: { width: innerWidth, height: innerHeight } });
  }), { message: 'Focused activation region and outline stay above dock (#373)' }).toBe('visible');
}

async function walkFocus(page: Page) {
  await page.locator('main').focus();
  let visited = 0;
  for (let step = 0; step < 150; step++) {
    await page.keyboard.press('Tab');
    if (!await page.evaluate(() => !!document.activeElement?.closest('main'))) break;
    await focusVisible(page);
    visited++;
  }
  expect(visited).toBeGreaterThanOrEqual(2);
  // Tab back from the dock to the last main control, then walk the reverse path.
  for (let step = 0; step < visited; step++) {
    await page.keyboard.press('Shift+Tab');
    expect(await page.evaluate(() => !!document.activeElement?.closest('main'))).toBe(true);
    await focusVisible(page);
  }
}

test('Correction and shared phone fields have 44px targets, 8px gaps and 16px editable text (#366, #367)', async ({ page }, info) => {
  await signIn(page);
  const edit = await openShellCorrection(page);
  for (const width of [320, 375, 393, 412]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of [edit, '/settings/alerts/', '/settings/ai/']) {
      await page.goto(path);
      await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
      await noOverflow(page, width);
      await fieldGeometry(page);
    }
    await page.goto(edit);
    const manage = page.getByRole('link', { name: 'Manage categories', exact: true });
    const box = await manage.boundingBox();
    expect(box!.height).toBeGreaterThanOrEqual(44);
    expect(box!.width).toBeGreaterThanOrEqual(44);
    await manage.click();
    await expect(page).toHaveURL(/settings\/categories\//);
    await page.goto('/planning/budgets/');
    await page.getByRole('button', { name: 'Add budget', exact: true }).filter({ visible: true }).click();
    await fieldGeometry(page);
    await page.keyboard.press('Escape');
  }
  await page.goto('/settings/alerts/');
  const toggle = page.getByLabel('Expected balance below threshold in the next 7 days', { exact: true });
  const before = await toggle.isChecked();
  const label = page.locator('label').filter({ has: toggle });
  expect((await label.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await label.click();
  expect(await toggle.isChecked()).toBe(!before);
});

test('Sequential focus stays above dock through portrait, landscape and keyboard resize (#373)', async ({ page }) => {
  test.setTimeout(240_000);
  await signIn(page);
  expect(await page.evaluate(() => 'ontouchstart' in window && matchMedia('(pointer: coarse)').matches)).toBe(true);
  const original = page.viewportSize()!;
  const edit = await openShellCorrection(page);
  for (const size of [original, { width: original.height, height: original.width }, { width: original.width, height: Math.round(original.height * 0.6) }]) {
    await page.setViewportSize(size);
    for (const path of [edit, '/settings/alerts/', '/settings/ai/', '/settings/categories/', '/more/']) {
      await page.goto(path);
      if ([edit, '/settings/alerts/', '/settings/ai/'].includes(path)) await fieldGeometry(page);
      await walkFocus(page);
    }
  }
  await page.goto(edit);
  const amount = page.getByLabel('Amount', { exact: true }).first();
  await amount.fill('invalid');
  await submit(page, page.getByRole('button', { name: 'Save correction', exact: true }));
  await expect(page.locator('main [role=alert]')).toContainText('Enter a number.');
  await amount.focus();
  await focusVisible(page);
  await noOverflow(page, original.width);
});

test('Long settings and CSV choice labels reflow with contained AI table (#365)', async ({ page }) => {
  await signIn(page);
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ['/settings/alerts/', '/settings/ai/']) {
      await page.goto(path);
      await noOverflow(page, width);
    }
  }
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
