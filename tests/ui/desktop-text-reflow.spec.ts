import { test, expect, signIn } from './fixtures';

// #369 and #370: these 200% cases now join the passing assertions, without retries/skips.
test('Budget rows and settings actions remain accessible with 200 percent text', async ({ page }, info) => {
  await signIn(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const path of ['/planning/budgets/', '/settings/categories/', '/settings/audit/']) {
    await page.goto(path);
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: path }).toBe(true);
    if (path.includes('budgets')) {
      const rows = page.getByRole('region', { name: 'Budget amounts and actions' });
      const attention = page.getByRole('heading', { name: 'Needs attention', exact: true });
      await expect(attention).toBeVisible();
      const bounds = await rows.boundingBox();
      const side = await attention.boundingBox();
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(side!.x);
      await expect(rows).toContainText('$500.00');
      await rows.focus();
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => rows.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
      const action = rows.getByLabel('Actions for Dining', { exact: true });
      await action.click();
      await expect(action.locator('..').getByRole('link', { name: 'Edit', exact: true })).toBeInViewport();
      const archive = action.locator('..').getByRole('button', { name: 'Archive', exact: true });
      await expect(archive).toBeInViewport();
      expect(await archive.evaluate(element => {
        const box = element.getBoundingClientRect();
        return element.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
      })).toBe(true);
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.keyboard.press('Escape');
      await page.getByRole('button', { name: 'Add budget', exact: true }).filter({ visible: true }).click();
      const dialog = page.locator('#add-budget-dialog');
      await expect(dialog.getByLabel('Monthly amount')).toBeVisible();
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    } else if (path.includes('categories')) {
      await expect(page.locator('#categories-heading')).toBeVisible();
      await expect(page.locator('main')).toContainText('Transportation');
      await page.locator('.category-rename > summary').first().click();
      await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Add category', exact: true })).toBeVisible();
      await expect(page.getByLabel('Match window (days)')).toBeVisible();
    } else {
      await expect(page.getByRole('region', { name: 'Audit events' })).toContainText('Member #1');
      await page.getByRole('region', { name: 'Audit events' }).focus();
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => page.getByRole('region', { name: 'Audit events' }).evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
      const lastColumn = page.getByRole('columnheader', { name: 'Event / run', exact: true });
      await lastColumn.scrollIntoViewIfNeeded();
      await expect(lastColumn).toBeInViewport();
      await page.getByRole('button', { name: 'Filter events', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Audit trail', exact: true })).toBeVisible();
      await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    }
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: path + ' actions' }).toBe(true);
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).fontSize)).toBe('32px');
  }
});
