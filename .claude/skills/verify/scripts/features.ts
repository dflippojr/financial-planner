import type { Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';

// features.json is shared with verify.py's --feature choices; SKILL.md's feature map
// documents each entry. `page` and `heading` are where the phone pass looks.
export const FEATURES: Record<string, { page: string; heading: string }> = JSON.parse(readFileSync(path.join(__dirname, 'features.json'), 'utf8'));
export type Feature = string;

const chosen = process.env.VERIFY_FEATURE || 'all';
if (chosen !== 'all' && !(chosen in FEATURES)) throw new Error('Unknown VERIFY_FEATURE ' + chosen);
export const selected = (feature: Feature) => {
  if (!(feature in FEATURES)) throw new Error('Feature missing from features.json: ' + feature);
  return chosen === 'all' || chosen === feature;
};

export async function shot(page: Page, device: 'desktop' | 'phone', feature: Feature) {
  await page.screenshot({ path: path.join(process.env.VERIFY_EVIDENCE!, 'screenshots', `${device}-${feature}.png`), fullPage: true });
}
