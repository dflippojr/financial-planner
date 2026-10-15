import { test as base, expect, type Page } from '@playwright/test';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createInterface } from 'node:readline';
import { mkdir, readFile, unlink, access } from 'node:fs/promises';
import path from 'node:path';

const execute = promisify(execFile);
process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1';
const python = process.env.UI_PYTHON || path.resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
export const test = base.extend<{ uiURL: string }>({
  uiURL: async ({}, use, testInfo) => {
    const registry = path.resolve('test-results/ui-instances', testInfo.testId.replace(/[^a-zA-Z0-9_-]/g, '_') + '.json');
    await mkdir(path.dirname(registry), { recursive: true });
    try {
      await access(registry);
      throw new Error('Previous cleanup handle remains; run scripts.cleanup_ui_tests before restarting');
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    const child = spawn(python, ['scripts/ui_instance.py', '--startup-handle', registry], { cwd: process.cwd(), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const exited = new Promise(resolve => child.once('close', resolve));
    let directory: string | undefined;
    let log = '';
    child.stderr.on('data', data => { log += data; });
    try {
      const instance = await new Promise<{ url: string; directory: string }>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Disposable UI startup timed out')), 60_000);
        const lines = createInterface({ input: child.stdout });
        lines.on('line', line => {
          try {
            const value = JSON.parse(line);
            if (value.url && value.directory) { clearTimeout(timer); resolve(value); }
          } catch { /* Ignore non-readiness output; never log credential JSON. */ }
        });
        child.once('error', error => { clearTimeout(timer); reject(error); });
        child.once('exit', () => { clearTimeout(timer); reject(new Error('Disposable startup failed: ' + log)); });
      });
      directory = instance.directory;
      const url = new URL(instance.url);
      if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.pathname !== '/') throw new Error('Non-disposable readiness URL');
      await use(url.origin);
    } finally {
      if (!directory) {
        try { directory = JSON.parse(await readFile(registry, 'utf8')).directory; }
        catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
      }
      if (directory && child.exitCode === null) {
        await execute(python, ['scripts/ui_instance.py', '--teardown', directory], { windowsHide: true, timeout: 15_000 });
      }
      await Promise.race([exited, new Promise((_, reject) => setTimeout(() => reject(new Error('Disposable process did not exit; cleanup handle retained')), 30_000).unref())]);
      if (directory) {
        await expect.poll(async () => { try { await access(directory!); return false; } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return true; throw error; } }).toBe(true);
        await unlink(registry);
      }
      expect(child.exitCode, 'Disposable instance exited cleanly').toBe(0);
    }
  },
  baseURL: async ({ uiURL }, use) => { await use(uiURL); },
});
export { expect };
export async function signIn(page: Page, user = 'synthetic_alex', password = 'Synthetic-passphrase-42!') {
  await page.goto('/sign-in/');
  await page.getByLabel('Username', { exact: true }).fill(user);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await submit(page, page.getByRole('button', { name: 'Sign in', exact: true }));
  await expect(page.getByRole('heading', { name: 'Cash flow', exact: true })).toBeVisible();
}
export async function noOverflow(page: Page, width: number) {
  await expect.poll(() => page.evaluate(() => innerWidth)).toBe(width);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}
export async function openShellCorrection(page: Page) {
  await page.goto('/transactions/');
  await page.getByRole('link', { name: /^(Correct )?Shell Oil 5741$/ }).filter({ visible: true }).click();
  await expect(page.getByRole('heading', { name: 'Correct transaction', exact: true })).toBeVisible();
  return new URL(page.url()).pathname;
}
export async function audit(page: Page, action: string, target: string, id: string, actor = '1') {
  await page.goto('/settings/audit/?action=' + action);
  const row = page.locator('tbody tr').filter({ hasText: target + ' #' + id }).filter({ hasText: 'Member #' + actor }).first();
  await expect(row).toContainText('Succeeded');
  await expect(row).toContainText('UI');
}

// Generic-profile upload and mapping of the synthetic CSV, stopping at Preview results.
export async function previewSyntheticCsv(page: Page) {
  await page.getByLabel('Import profile').selectOption('generic');
  await page.getByLabel('CSV, OFX or QFX file').setInputFiles('tests/fixtures/ui-pass/synthetic.csv');
  await submit(page, page.getByRole('button', { name: 'Upload and map' }));
  for (const [name, value] of [['date_column', 'Date'], ['description_column', 'Description'], ['amount_column', 'Amount'], ['currency_column', 'Currency']]) await page.locator('[name=' + name + ']').selectOption(value);
  await page.locator('[name=date_format]').selectOption('iso');
  await page.locator('[name=number_format]').selectOption('dot_none');
  await page.getByLabel('One signed amount column').check();
  await submit(page, page.getByRole('button', { name: 'Preview rows' }));
}

export async function submit(page: Page, button: import('@playwright/test').Locator) {
  // WebKit clicks can return before a form navigation commits. Wait before reloading.
  await Promise.all([page.waitForEvent('framenavigated', { predicate: frame => frame === page.mainFrame() }), button.click()]);
  await page.waitForLoadState('domcontentloaded');
}
