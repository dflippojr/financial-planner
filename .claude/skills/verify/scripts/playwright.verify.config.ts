import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

// Driven by verify.py against the instance it launched; never starts its own server.
const evidence = process.env.VERIFY_EVIDENCE;
const url = process.env.VERIFY_URL;
if (!evidence || !url) throw new Error('Run through .claude/skills/verify/scripts/verify.py');
const origin = new URL(url);
if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || !origin.port) throw new Error('Non-disposable VERIFY_URL');

export default defineConfig({
  testDir: __dirname, testMatch: /\.verify\.ts$/,
  // One writer at a time on the seeded rows, per docs/ui-testing.md "Seed and sign-in".
  fullyParallel: false, workers: 1, retries: 0,
  timeout: 120_000, expect: { timeout: 10_000 },
  outputDir: path.join(evidence, 'test-results'),
  reporter: [['list'], ['json', { outputFile: path.join(evidence, 'drive-results.json') }]],
  use: { baseURL: origin.origin, actionTimeout: 15_000, navigationTimeout: 20_000, trace: 'off', screenshot: 'off', video: 'off' },
  projects: [
    { name: 'desktop', testMatch: /drive\.verify\.ts$/, use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    // Read-only pass after the desktop writes; Chromium keeps the run to one browser install.
    { name: 'phone', testMatch: /phone\.verify\.ts$/, dependencies: ['desktop'], use: { ...devices['Pixel 7'] } },
  ],
});
