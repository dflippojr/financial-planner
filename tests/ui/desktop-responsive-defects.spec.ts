import { devices, chromium, webkit, type Locator, type Page } from '@playwright/test';
import { test, expect, signIn, submit } from './fixtures';

async function withDevice(device: string, viewport: { width: number; height: number }, colorScheme: 'light' | 'dark', baseURL: string, check: (page: Page) => Promise<void>) {
  const browser = await (device.startsWith('iPhone') ? webkit : chromium).launch();
  try {
    const context = await browser.newContext({ ...devices[device], viewport, colorScheme, baseURL });
    try { await check(await context.newPage()); }
    finally { await context.close(); }
  } finally { await browser.close(); }
}

async function reachable(control: Locator) {
  await control.evaluate(element => element.scrollIntoView({ block: 'start' }));
  try {
    await expect.poll(() => control.evaluate(element => {
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return { visible: rect.y >= 0 && rect.bottom <= innerHeight, receivesHit: !!hit && element.contains(hit) };
    }), { message: 'Reachable ' + await control.evaluate(el => el.tagName) }).toMatchObject({ visible: true, receivesHit: true });
  } catch (error) {
    const geometry = await control.evaluate(element => {
      const rect = element.getBoundingClientRect();
      const panel = element.closest('.filters-panel');
      return {
        top: rect.top, bottom: rect.bottom, width: innerWidth, height: innerHeight, scroll: scrollY,
        panel: panel ? {
          top: panel.getBoundingClientRect().top, height: panel.getBoundingClientRect().height,
          scrollTop: panel.scrollTop, scrollHeight: panel.scrollHeight, position: getComputedStyle(panel).position,
        } : null,
      };
    });
    throw new Error('Unreachable control geometry: ' + JSON.stringify(geometry), { cause: error });
  }
  await control.click({ trial: true });
}

async function answer(page: Page, prompt: string) {
  const replies = page.locator('main').getByText('Synthetic UI reply. No real provider was contacted.', { exact: true });
  const before = await replies.count();
  await page.locator('main').getByLabel('Question', { exact: true }).fill(prompt);
  await submit(page, page.locator('main').getByRole('button', { name: 'Send', exact: true }));
  await expect(replies).toHaveCount(before + 1, { timeout: 20_000 });
  await expect(page.locator('main')).toContainText('Synthetic UI reply. No real provider was contacted.');
}

for (const device of ['iPhone SE', 'iPhone 15', 'Pixel 7']) {
  for (const landscape of [false, true]) {
    test.describe(device + (landscape ? ' landscape' : ' portrait'), () => {
      const size = device === 'iPhone SE' ? { width: 375, height: 667 } : devices[device].screen!;
      const viewport = landscape ? { width: size.height, height: size.width } : size;
      test('#371 Chat delete is reachable for short, long and newly empty threads', async ({ uiURL }, info) => {
        await withDevice(device, viewport, info.project.use.colorScheme as 'light' | 'dark', uiURL, async page => {
          await signIn(page);
          expect(await page.evaluate(() => ({ width: innerWidth, height: innerHeight }))).toEqual(viewport);
          await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
          await page.goto('/chat/');
          const remove = page.locator('main form[action="/chat/delete-all/"] button').filter({ visible: true });
          await answer(page, 'Synthetic short conversation');
          await page.reload();
          await reachable(remove);
          expect((await remove.boundingBox())!.height).toBeGreaterThanOrEqual(44);
          page.once('dialog', dialog => dialog.dismiss());
          await remove.tap();
          await page.reload();
          await expect(page.locator('main')).toContainText('Synthetic short conversation');
          await answer(page, 'Synthetic long conversation ' + 'synthetic detail '.repeat(120));
          await reachable(remove);
          await remove.focus();
          page.once('dialog', dialog => dialog.dismiss());
          await remove.press('Enter');
          await submit(page, page.getByRole('button', { name: 'New conversation', exact: true }).filter({ visible: true }));
          const question = page.locator('main').getByLabel('Question', { exact: true });
          await expect(question).toHaveValue('');
          await page.setViewportSize({ width: viewport.width, height: Math.round(viewport.height * 0.6) });
          await question.focus();
          await reachable(question);
          await reachable(page.locator('main').getByRole('button', { name: 'Send', exact: true }));
          await reachable(remove);
          await page.setViewportSize(viewport);
          await reachable(page.getByRole('navigation', { name: 'Main tabs', exact: true }).getByRole('link').first());
          page.once('dialog', dialog => dialog.accept());
          await submit(page, remove);
          await page.reload();
          await expect(page.getByRole('navigation', { name: 'Conversations', exact: true }).locator('li')).toHaveCount(0);
          await expect(page.locator('main')).not.toContainText('Synthetic short conversation');
          await expect(page.locator('main')).not.toContainText('Synthetic long conversation');
        });
      });
    });
  }
}

for (const device of ['iPhone 15', 'Pixel 7', 'Desktop Chrome']) {
  test.describe(device + ' short Home filters', () => {
    const descriptor = devices[device];
    const viewport = device === 'Desktop Chrome' ? { width: 1024, height: 500 } : { width: descriptor.screen!.height, height: descriptor.screen!.width };
    test('#375 invalid dates reset and valid dates survive rotation', async ({ uiURL }, info) => {
      await withDevice(device, viewport, info.project.use.colorScheme as 'light' | 'dark', uiURL, async page => {
        await signIn(page);
        await expect(page.locator('html')).toHaveAttribute('data-theme', info.project.use.colorScheme as string);
        const panel = page.locator('#cash-flow-filters');
        const from = panel.locator('[name=date_from]');
        const to = panel.locator('[name=date_to]');
        await panel.locator('summary').click();
        await from.fill('2026-10-05');
        await to.fill('2026-10-04');
        await submit(page, panel.getByRole('button', { name: 'Update', exact: true }));
        await expect(panel).toContainText('End date must be on or after start date.');
        const close = panel.getByRole('button', { name: 'Close filters', exact: true });
        await reachable(close);
        await close.click();
        await expect(panel).not.toHaveAttribute('open', '');
        await expect(panel.locator('summary')).toBeFocused();
        await panel.locator('summary').press('Enter');
        await expect(from).toHaveValue('2026-10-05');
        await expect(to).toHaveValue('2026-10-04');
        await expect(panel).toContainText('End date must be on or after start date.');
        const reset = panel.getByRole('link', { name: 'Reset range', exact: true });
        await reachable(reset);
        await reset.focus();
        await expect(reset).toBeFocused();
        await Promise.all([page.waitForEvent('framenavigated', { predicate: frame => frame === page.mainFrame() }), reset.press('Enter')]);
        await page.waitForLoadState('domcontentloaded');
        await page.reload();
        await expect(page).not.toHaveURL(/date_(from|to)=/);
        await panel.locator('summary').click();
        await from.fill('2026-10-01');
        await to.fill('2026-10-05');
        await from.focus();
        await page.setViewportSize({ width: viewport.height, height: viewport.width });
        await expect(from).toHaveValue('2026-10-01');
        await expect(to).toHaveValue('2026-10-05');
        await expect(from).toBeFocused();
        await page.setViewportSize(viewport);
        await from.press('Escape');
        await expect(panel).not.toHaveAttribute('open', '');
        await expect(panel.locator('summary')).toBeFocused();
        await panel.locator('summary').press('Enter');
        await expect(from).toHaveValue('2026-10-01');
        await expect(to).toHaveValue('2026-10-05');
        await reachable(panel.getByRole('button', { name: 'Update', exact: true }));
        await submit(page, panel.getByRole('button', { name: 'Update', exact: true }));
        await page.reload();
        await expect(page).toHaveURL(/date_from=2026-10-01/);
        await expect(page).toHaveURL(/date_to=2026-10-05/);
        await panel.locator('summary').click();
        await reachable(reset);
        await submit(page, reset);
        await page.reload();
        await expect(page).not.toHaveURL(/date_(from|to)=/);
      });
    });
  });
}
