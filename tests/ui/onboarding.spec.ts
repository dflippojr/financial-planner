import { devices } from '@playwright/test';
import { test, expect, signIn, submit } from './fixtures';
import type { Locator } from '@playwright/test';

async function enterRuntimeCode(input: Locator, code: string) {
  // fill(code) embeds its argument in the HTML report's step title. evaluate
  // reports only the method name; runtime arguments remain inside the browser.
  await input.evaluate((element: HTMLInputElement, value) => {
    element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  }, code);
}

// No screenshots, traces, DOM snapshots or code-bearing assertion diagnostics.
// Codes stay in memory, and errors in the sensitive section are replaced below.
test('Invitation join and one-time recovery revoke the guest session', async ({ page, browser, uiURL }, info) => {
  await signIn(page);
  await page.goto('/settings/household/');
  let invitation = '', recovery = '', invitationID = '', stage = 'create invitation';
  const contextOptions = { ...(info.project.name.startsWith('phone') ? devices['iPhone SE'] : devices['Desktop Chrome']), baseURL: uiURL, colorScheme: info.project.use.colorScheme };
  const guestContext = await browser.newContext(contextOptions);
  const recoveryContext = await browser.newContext(contextOptions);
  try {
    await submit(page, page.getByRole('button', { name: 'Create invitation code', exact: true }));
    if (new URL(page.url()).pathname === '/reauth/') {
      await page.getByLabel('Password', { exact: true }).fill('Synthetic-passphrase-42!');
      await submit(page, page.getByRole('button', { name: 'Confirm', exact: true }));
      await submit(page, page.getByRole('button', { name: 'Create invitation code', exact: true }));
    }
    invitation = (await page.locator('#invitation-code').textContent())!.trim();
    if (!invitation) throw new Error('Missing invitation');
    await page.goto('/settings/household/');
    await page.reload();
    if (await page.locator('#invitation-code').count()) throw new Error('Invitation redisplayed');
    await page.goto('/settings/audit/?action=invitation_created');
    const created = page.locator('tbody tr').filter({ hasText: 'Invitation created / Succeeded' }).filter({ hasText: 'Member #1' }).first();
    invitationID = ((await created.locator('td').nth(3).innerText()).match(/invitation #(\d+)/) || [])[1];
    if (!invitationID) throw new Error('Missing invitation audit');
    stage = 'join guest';
    const guest = await guestContext.newPage();
    await guest.goto('/join/');
    await guest.getByLabel('Invitation code', { exact: true }).fill('INVALID-SYNTHETIC-INVITATION');
    await guest.getByLabel('Username', { exact: true }).fill('synthetic_ui_guest');
    await guest.getByLabel('Display name', { exact: true }).fill('Synthetic UI Guest');
    await guest.getByLabel('New password', { exact: true }).fill('Synthetic-guest-passphrase-72!');
    await guest.getByLabel('Confirm new password', { exact: true }).fill('Synthetic-guest-passphrase-72!');
    await guest.getByLabel('I accept the privacy and data policy', { exact: true }).check();
    await submit(guest, guest.getByRole('button', { name: 'Create account', exact: true }));
    if (await guest.locator('#recovery-codes').count()) throw new Error('Invalid invitation accepted');
    await enterRuntimeCode(guest.getByLabel('Invitation code', { exact: true }), invitation);
    await guest.getByLabel('New password', { exact: true }).fill('Synthetic-guest-passphrase-72!');
    await guest.getByLabel('Confirm new password', { exact: true }).fill('Synthetic-guest-passphrase-72!');
    await submit(guest, guest.getByRole('button', { name: 'Create account', exact: true }));
    stage = 'read recovery codes';
    const codes = (await guest.locator('#recovery-codes').textContent())!.trim().split(/\s+/);
    if (codes.length !== 8) throw new Error('Recovery code count');
    recovery = codes[0];
    codes.fill(''); invitation = '';
    stage = 'one-time redisplay';
    await guest.goto('/join/');
    await guest.reload();
    if (await guest.locator('#recovery-codes').count()) throw new Error('Recovery codes redisplayed');
    stage = 'guest login';
    await signIn(guest, 'synthetic_ui_guest', 'Synthetic-guest-passphrase-72!');
    stage = 'recover guest';
    const recover = await recoveryContext.newPage();
    await recover.goto('/recover/');
    await recover.getByLabel('Username', { exact: true }).fill('synthetic_ui_guest');
    await enterRuntimeCode(recover.getByLabel('Recovery code', { exact: true }), recovery);
    await recover.getByLabel('New password', { exact: true }).fill('Synthetic-recovered-passphrase-83!');
    await recover.getByLabel('Confirm new password', { exact: true }).fill('Synthetic-recovered-passphrase-83!');
    await submit(recover, recover.getByRole('button', { name: 'Update password', exact: true }));
    if (!(await recover.locator('main').innerText()).includes('Password updated')) throw new Error('Recovery failed');
    stage = 'session revocation';
    await guest.goto('/transactions/');
    if (new URL(guest.url()).pathname !== '/sign-in/') throw new Error('Prior session not revoked');
    stage = 'old password rejected';
    await guest.getByLabel('Username', { exact: true }).fill('synthetic_ui_guest');
    await guest.getByLabel('Password', { exact: true }).fill('Synthetic-guest-passphrase-72!');
    await submit(guest, guest.getByRole('button', { name: 'Sign in', exact: true }));
    if (new URL(guest.url()).pathname !== '/sign-in/') throw new Error('Old password accepted');
    stage = 'new password login';
    await signIn(guest, 'synthetic_ui_guest', 'Synthetic-recovered-passphrase-83!');
    await recover.goto('/recover/');
    await recover.getByLabel('Username', { exact: true }).fill('synthetic_ui_guest');
    await enterRuntimeCode(recover.getByLabel('Recovery code', { exact: true }), recovery);
    await recover.getByLabel('New password', { exact: true }).fill('Synthetic-another-passphrase-94!');
    await recover.getByLabel('Confirm new password', { exact: true }).fill('Synthetic-another-passphrase-94!');
    await submit(recover, recover.getByRole('button', { name: 'Update password', exact: true }));
    if ((await recover.locator('main').innerText()).includes('Password updated')) throw new Error('Code reused');
    await recover.locator('input[name=recovery_code]').fill('');
    recovery = '';
    stage = 'household audit';
    await page.goto('/settings/household/');
    await expect(page.locator('main')).toContainText('Synthetic UI Guest');
    await page.goto('/settings/audit/?action=invitation_accepted');
    const accepted = page.locator('tbody tr').filter({ hasText: 'invitation #' + invitationID }).filter({ hasText: 'Invitation accepted / Succeeded' }).first();
    const member = ((await accepted.locator('td').nth(2).innerText()).match(/Member #(\d+)/) || [])[1];
    if (!member) throw new Error('Missing guest actor');
    await expect(accepted).toContainText('affecting member #' + member);
    await guest.goto('/settings/audit/?action=password_changed');
    await expect(guest.locator('tbody tr').filter({ hasText: 'Member #' + member }).filter({ hasText: 'member #' + member }).first()).toContainText('Password changed / Succeeded');
    stage = 'security event';
    await guest.goto('/settings/security/');
    await expect(guest.locator('main')).toContainText('Password changed');
  } catch (error) {
    // Deliberately omit locator call logs and DOM contents from this sensitive flow.
    throw new Error('Synthetic invitation/recovery journey failed at ' + stage + '; code capture disabled (' + (error instanceof Error ? error.name : 'Error') + ')');
  } finally {
    invitation = ''; recovery = '';
    await guestContext.close(); await recoveryContext.close();
    await page.goto('/settings/security/');
  }
});
