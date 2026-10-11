import { test, expect, signIn, openShellCorrection } from './fixtures';

// Failing AI/alert-settings reflow is tracked by #365.
// Those pages are not asserted passing, skipped, or hidden with retries.
const pages = [
  ['Home', '/'], ['Activity', '/transactions/'], ['Budgets', '/planning/budgets/'],
  ['Alerts', '/alerts/'], ['Chat', '/chat/'], ['Security', '/settings/security/'],
  ['Categories', '/settings/categories/'], ['Tags', '/settings/tags/'],
  ['Data', '/settings/data/'], ['Audit', '/settings/audit/'],
  ['Household', '/settings/household/'], ['Import', '/imports/'], ['More', '/more/'],
];

test('Passing primary pages reflow at 320/375/768/1440 CSS pixels', async ({ page }, info) => {
  test.setTimeout(180_000);
  await signIn(page);
  const edit = await openShellCorrection(page);
  for (const [name, path] of [...pages, ['Correction', edit]]) {
    const response = await page.goto(path);
    expect(response!.status(), name).toBe(200);
    await expect(page.locator('main h1')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await expect.poll(() => page.evaluate(() => innerWidth), { message: name + ' effective CSS viewport' }).toBe(width);
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: name + ' horizontal overflow at ' + width }).toBe(true);
    }
  }
  // A fresh unauthenticated context keeps public entry/recovery forms code-free.
  const publicContext = await page.context().browser()!.newContext({ baseURL: new URL(page.url()).origin, colorScheme: info.project.use.colorScheme });
  try {
    const publicPage = await publicContext.newPage();
    for (const path of ['/sign-in/', '/join/', '/recover/']) {
      await publicPage.goto(path);
      await expect(publicPage.locator('main h1')).toBeVisible();
      for (const width of [320, 375, 768, 1440]) {
        await publicPage.setViewportSize({ width, height: 900 });
        await expect.poll(() => publicPage.evaluate(() => innerWidth)).toBe(width);
        await expect.poll(() => publicPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      }
    }
  } finally { await publicContext.close(); }
});
