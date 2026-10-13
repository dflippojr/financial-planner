import { test, expect } from '@playwright/test';
import { signIn } from '../../../../tests/ui/fixtures';
import { FEATURES, selected, shot } from './features';

// Read-only phone screenshots after the desktop drive has made its writes.
for (const feature of Object.keys(FEATURES)) {
  if (!selected(feature)) continue;
  test(`${feature}: phone screenshot`, async ({ page }) => {
    await signIn(page);
    await page.goto(FEATURES[feature].page);
    await expect(page.getByRole('heading', { name: FEATURES[feature].heading, exact: true }).filter({ visible: true })).toBeVisible();
    await shot(page, 'phone', feature);
  });
}
