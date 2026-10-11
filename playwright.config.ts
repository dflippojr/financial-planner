import { defineConfig, devices } from '@playwright/test';

// Recovery/invitation codes must never appear in failure DOM snapshots.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1';
export default defineConfig({
  testDir: './tests/ui', fullyParallel: true, workers: 2, retries: 0,
  timeout: 120_000, expect: { timeout: 10_000 },
  outputDir: 'test-results/ui',
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { actionTimeout: 15_000, navigationTimeout: 20_000, trace: 'off', screenshot: 'off', video: 'off' },
  projects: [
    { name: 'desktop-light', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, colorScheme: 'light' } },
    { name: 'desktop-dark', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, colorScheme: 'dark' } },
    { name: 'phone-light', testIgnore: /desktop-resize/, use: { ...devices['iPhone SE'], browserName: 'webkit', colorScheme: 'light' } },
    { name: 'phone-dark', testIgnore: /desktop-resize/, use: { ...devices['iPhone SE'], browserName: 'webkit', colorScheme: 'dark' } },
  ],
});
