import { defineConfig, devices } from '@playwright/test';

// Recovery/invitation codes must never appear in failure DOM snapshots.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1';
export default defineConfig({
  // CI otherwise embeds the entire PR patch, including the privacy tests'
  // deliberately code-shaped synthetic sentinels. Evidence needs the commit,
  // not source patches. Keep the privacy gate strict for every report value.
  captureGitInfo: { commit: true, diff: false },
  testDir: './tests/ui', fullyParallel: true, workers: 2, retries: 0,
  timeout: 120_000, expect: { timeout: 10_000 },
  outputDir: 'test-results/ui',
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { actionTimeout: 15_000, navigationTimeout: 20_000, trace: 'off', screenshot: 'off', video: 'off' },
  projects: [
    ...(['iPhone SE', 'iPhone 15', 'Pixel 7'] as const).flatMap(device =>
      (['light', 'dark'] as const).map(colorScheme => ({
        name: 'forms-' + device.replaceAll(' ', '-') + '-' + colorScheme,
        testMatch: /phone-forms\.spec\.ts/,
        use: { ...devices[device], viewport: device === 'iPhone SE' ? { width: 375, height: 667 } : device === 'iPhone 15' ? { width: 393, height: 852 } : { width: 412, height: 915 }, colorScheme },
      }))),
    { name: 'desktop-light', testIgnore: /phone-forms\.spec\.ts/, use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, colorScheme: 'light' } },
    { name: 'desktop-dark', testIgnore: /phone-forms\.spec\.ts/, use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, colorScheme: 'dark' } },
    { name: 'phone-light', testIgnore: /desktop-|phone-forms\.spec\.ts/, use: { ...devices['iPhone SE'], browserName: 'webkit', colorScheme: 'light' } },
    { name: 'phone-dark', testIgnore: /desktop-|phone-forms\.spec\.ts/, use: { ...devices['iPhone SE'], browserName: 'webkit', colorScheme: 'dark' } },
  ],
});
