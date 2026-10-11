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
