import type { Page } from '@playwright/test';
import path from 'node:path';

// Keep in step with the feature map in ../SKILL.md. `page` is where the phone pass looks.
export const FEATURES = {
  import: { page: '/transactions/', heading: 'Transactions' },
  cashflow: { page: '/', heading: 'Cash flow' },
  recurring: { page: '/recurring/', heading: 'Recurring charges' },
  dashboard: { page: '/', heading: 'Cash flow' },
  alert: { page: '/alerts/', heading: 'Alerts' },
} as const;
export type Feature = keyof typeof FEATURES;

const chosen = process.env.VERIFY_FEATURE || 'all';
if (chosen !== 'all' && !(chosen in FEATURES)) throw new Error('Unknown VERIFY_FEATURE ' + chosen);
export const selected = (feature: Feature) => chosen === 'all' || chosen === feature;

export async function shot(page: Page, device: 'desktop' | 'phone', feature: Feature) {
  await page.screenshot({ path: path.join(process.env.VERIFY_EVIDENCE!, 'screenshots', `${device}-${feature}.png`), fullPage: true });
}
