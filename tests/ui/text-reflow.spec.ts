import { test, expect, signIn } from './fixtures';

// #368: assert the previously untested enlarged-text branch on desktop and iPhone SE.
test('Home values and expanded chart reflow with 200 percent root text size', async ({ page }, info) => {
  await signIn(page);
  const phone = info.project.name.startsWith('phone');
  await page.setViewportSize({ width: phone ? 375 : 1280, height: phone ? 667 : 900 });
  const amounts = page.locator('[aria-label="Key numbers"] p > span');
  await expect(amounts).toHaveCount(3);
  const originalAmounts = await amounts.allTextContents();
  await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
  await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
  if (phone) await page.getByText('Income, spending, and net by period', { exact: true }).click();
  const chart = page.locator('#cash-flow-chart');
  await expect(chart).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const canvas = document.querySelector<HTMLCanvasElement>('#cash-flow-chart')!;
    const box = canvas.getBoundingClientRect();
    const parent = canvas.parentElement!.getBoundingClientRect();
    return box.width > 0 && box.left >= parent.left && box.right <= parent.right && box.right <= innerWidth
      && Math.abs(canvas.width / devicePixelRatio - box.width) < 1;
  })).toBe(true);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(amounts).toHaveText(originalAmounts);
  expect(await amounts.evaluateAll(elements => elements.every(element => {
    const box = element.getBoundingClientRect();
    const parent = element.parentElement!.getBoundingClientRect();
    return box.left >= parent.left && box.right <= parent.right;
  }))).toBe(true);
  await page.locator('#cash-flow-filters > summary').click();
  await expect(page.getByRole('button', { name: 'Update', exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).fontSize)).toBe('32px');
});

// #390: use each project's browser at the same 320px viewport in both themes.
test('320px Home filters reflow with 200 percent text and a reachable close action', async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 667 });
  await signIn(page);
  const amounts = page.locator('[aria-label="Key numbers"] p > span');
  const originalAmounts = await amounts.allTextContents();
  await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
  await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
  const panel = page.locator('#cash-flow-filters');
  const summary = panel.locator('summary');
  await summary.click();
  const close = panel.getByRole('button', { name: 'Close filters', exact: true });
  await close.scrollIntoViewIfNeeded();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect.poll(() => close.evaluate(element => {
    const header = element.closest('[data-filters-header]')!.getBoundingClientRect();
    const box = element.getBoundingClientRect();
    const dock = document.querySelector('nav.dock')!.getBoundingClientRect();
    return {
      headerContained: header.left >= 0 && header.right <= innerWidth,
      actionContained: box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= dock.top,
      touchTarget: box.width >= 44 && box.height >= 44,
      receivesHits: [box.top + 2, box.top + box.height / 2, box.bottom - 2]
        .every(y => element.contains(document.elementFromPoint(box.left + box.width / 2, y))),
    };
  })).toEqual({ headerContained: true, actionContained: true, touchTarget: true, receivesHits: true });
  await expect(amounts).toHaveText(originalAmounts);
  await close.click();
  await expect(panel).not.toHaveAttribute('open', '');
  await expect(summary).toBeFocused();
  await summary.press('Enter');
  await expect(close).toBeVisible();
  await close.focus();
  await close.press('Enter');
  await expect(panel).not.toHaveAttribute('open', '');
  await expect(summary).toBeFocused();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).fontSize)).toBe('32px');
});
