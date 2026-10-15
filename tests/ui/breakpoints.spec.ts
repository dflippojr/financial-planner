import { test, expect, signIn, openShellCorrection } from './fixtures';

const pages = [
  ['Home', '/'], ['Activity', '/transactions/'], ['Budgets', '/planning/budgets/'],
  ['Alerts', '/alerts/'], ['Chat', '/chat/'], ['Security', '/settings/security/'],
  ['Categories', '/settings/categories/'], ['Tags', '/settings/tags/'],
  ['Data', '/settings/data/'], ['Audit', '/settings/audit/'],
  ['AI settings (#365)', '/settings/ai/'], ['Alert settings (#365)', '/settings/alerts/'],
  ['Household', '/settings/household/'], ['Import', '/imports/'], ['More', '/more/'],
  ['Accounts', '/accounts/'], ['Net worth', '/net-worth/'], ['Spending', '/spending/'],
  ['Recurring', '/recurring/'], ['Transfers', '/transfers/'], ['Bills', '/planning/calendar/'],
  ['Goals', '/planning/goals/'], ['Planned items', '/planning/items/'],
  ['Monthly review', '/planning/review/'], ['Year-end', '/planning/year-end/'],
  ['Connections', '/settings/connections/'], ['Category rules', '/settings/categories/rules/'],
  ['CSV mappings', '/settings/csv-mappings/'],
];
const widths = [320, 375, 768, 1024, 1280, 1440];

// Clipped button text and 44px dock and primary-button targets on the phone layout.
const phoneLayout = () => {
  const clipped = [...document.querySelectorAll('.btn, .badge, [role=button], button')]
    .filter(el => (el as HTMLElement).offsetParent !== null && el.scrollWidth > el.clientWidth + 1)
    .map(el => (el.textContent || el.getAttribute('aria-label') || el.className).trim().slice(0, 40));
  const small = [...document.querySelectorAll('.dock a, .dock button, main .btn-primary')]
    .filter(el => (el as HTMLElement).offsetParent !== null || getComputedStyle(el).position === 'fixed')
    .map(el => { const r = el.getBoundingClientRect(); return [(el.textContent || el.getAttribute('aria-label') || '').trim(), r.width, r.height] as const; })
    .filter(([, w, h]) => w > 0 && (w < 44 || h < 44));
  return { clipped, small };
};

test('Passing primary pages reflow at 320/375/768/1024/1280/1440 CSS pixels', async ({ page }, info) => {
  test.setTimeout(240_000);
  const phone = info.project.name.startsWith('phone');
  await signIn(page);
  const edit = await openShellCorrection(page);
  for (const [name, path] of [...pages, ['Correction', edit]]) {
    const response = await page.goto(path);
    expect(response!.status(), name).toBe(200);
    await expect(page.locator('main h1')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
    for (const width of widths) {
      await page.setViewportSize({ width, height: 900 });
      await expect.poll(() => page.evaluate(() => innerWidth), { message: name + ' effective CSS viewport' }).toBe(width);
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), { message: name + ' horizontal overflow at ' + width }).toBe(true);
      if (phone && width === 375) {
        expect(await page.evaluate(phoneLayout), name + ' clipped button text and 44px targets at 375').toEqual({ clipped: [], small: [] });
      }
    }
  }
  // A fresh unauthenticated context keeps public entry/recovery forms code-free.
  const publicContext = await page.context().browser()!.newContext({ baseURL: new URL(page.url()).origin, colorScheme: info.project.use.colorScheme });
  try {
    const publicPage = await publicContext.newPage();
    for (const path of ['/sign-in/', '/join/', '/recover/']) {
      await publicPage.goto(path);
      await expect(publicPage.locator('main h1')).toBeVisible();
      for (const width of widths) {
        await publicPage.setViewportSize({ width, height: 900 });
        await expect.poll(() => publicPage.evaluate(() => innerWidth)).toBe(width);
        await expect.poll(() => publicPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      }
    }
  } finally { await publicContext.close(); }
});
